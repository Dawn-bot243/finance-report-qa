"""PDF 解析方案对比：pymupdf / pdfplumber / 多模态文档解析模型（PaddleOCR-VL、DeepSeek-OCR）。

评测口径与质检 gate2 相同：CFQA 有效题的答案文本/数字是否出现在标注页码上
（复用 src.qa.docmap.answer_hits_page，只查标注页本身，不做 ±2 窗口）。

用法：
    python -X utf8 scripts/parse_compare.py --limit 20          # 小样本试跑
    python -X utf8 scripts/parse_compare.py                     # 全量 426 页
    python -X utf8 scripts/parse_compare.py --sources pymupdf,pdfplumber  # 只跑本地
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures as cf
import json
import os
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pymupdf
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.qa.docmap import answer_hits_page  # noqa: E402

CACHE = ROOT / "db" / "parse_compare"
PNG_DIR = CACHE / "pages"
OCR_MODELS = {
    "paddleocr": "PaddlePaddle/PaddleOCR-VL-1.5",
    "deepseek_ocr": "deepseek-ai/DeepSeek-OCR",
}
OCR_PROMPTS = {
    "paddleocr": "<image>OCR:",
    "deepseek_ocr": "OCR the image to markdown.",
}
DPI = 150


# ---------------------------------------------------------------- 数据
def load_questions(conn: sqlite3.Connection) -> list[dict]:
    """有效题 + 标注页码。"""
    rows = conn.execute(
        "SELECT question_id, code, answer, qa_doc_map, q_year FROM cfqa_questions "
        "WHERE qa_check!='skip' AND name_ok=1 ORDER BY question_id"
    ).fetchall()
    out = []
    for r in rows:
        m = json.loads(r["qa_doc_map"])
        groups = [g["pages"] for g in m.get("groups", []) if g.get("pages")]
        if not groups:
            continue
        out.append({"qid": r["question_id"], "code": r["code"], "answer": r["answer"],
                    "doc_id": m["doc_id"], "groups": groups, "year": r["q_year"]})
    return out


def collect_pages(qs: list[dict]) -> list[tuple[int, int]]:
    return sorted({(q["doc_id"], p) for q in qs for g in q["groups"] for p in g})


def doc_paths(conn: sqlite3.Connection) -> dict[int, str]:
    return {r["doc_id"]: r["file_path"] for r in
            conn.execute("SELECT doc_id, file_path FROM documents")}


# ---------------------------------------------------------------- 提取：pymupdf（现状）
def extract_pymupdf(conn: sqlite3.Connection, pages: list[tuple[int, int]]) -> dict:
    out = {}
    for did, pg in pages:
        r = conn.execute("SELECT text FROM pages WHERE doc_id=? AND page_no=?",
                         (did, pg)).fetchone()
        out[(did, pg)] = r["text"] if r else ""
    return out


# ---------------------------------------------------------------- 提取：pdfplumber
def extract_pdfplumber(paths: dict[int, str], pages: list[tuple[int, int]]) -> dict:
    import pdfplumber
    by_doc: dict[int, list[int]] = {}
    for did, pg in pages:
        by_doc.setdefault(did, []).append(pg)
    out: dict[tuple[int, int], str] = {}
    for did, pgs in by_doc.items():
        cache_file = CACHE / "pdfplumber" / f"{did}.json"
        cached = json.loads(cache_file.read_text(encoding="utf-8")) if cache_file.exists() else {}
        todo = [p for p in pgs if str(p) not in cached]
        if todo:
            with pdfplumber.open(paths[did]) as pdf:
                for p in todo:
                    try:
                        txt = pdf.pages[p - 1].extract_text() or ""
                    except Exception as e:  # 单页失败不影响整体
                        print(f"  [pdfplumber] doc{did} p{p} 失败: {e}")
                        txt = ""
                    cached[str(p)] = txt
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(cached, ensure_ascii=False), encoding="utf-8")
        for p in pgs:
            out[(did, p)] = cached.get(str(p), "")
        print(f"  [pdfplumber] doc{did}: {len(pgs)} 页完成")
    return out


# ---------------------------------------------------------------- 提取：多模态 OCR
def render_pngs(paths: dict[int, str], pages: list[tuple[int, int]]) -> dict:
    """预渲染页面为 PNG，避免多线程重复打开 PDF。"""
    PNG_DIR.mkdir(parents=True, exist_ok=True)
    by_doc: dict[int, list[int]] = {}
    for did, pg in pages:
        by_doc.setdefault(did, []).append(pg)
    out = {}
    for did, pgs in by_doc.items():
        doc = pymupdf.open(paths[did])
        for p in pgs:
            f = PNG_DIR / f"{did}_{p}.png"
            if not f.exists():
                f.write_bytes(doc[p - 1].get_pixmap(dpi=DPI).tobytes("png"))
            out[(did, p)] = f
        doc.close()
        print(f"  [render] doc{did}: {len(pgs)} 页")
    return out


_lock = threading.Lock()


def _ocr_one(args) -> tuple[tuple[int, int], str, str]:
    key, png, model, prompt, api_key, retries = args
    b64 = base64.b64encode(png.read_bytes()).decode()
    body = {
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}},
            {"type": "text", "text": prompt},
        ]}],
        "max_tokens": 6144,
        "temperature": 0,
    }
    last = ""
    for attempt in range(retries):
        try:
            r = requests.post(
                "https://api.siliconflow.cn/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json=body, timeout=180)
            if r.status_code == 200:
                return key, r.json()["choices"][0]["message"]["content"] or "", ""
            last = f"HTTP {r.status_code}: {r.text[:200]}"
            # TPM 限流：退避更久，避免把限流失败误算成模型能力不足
            if r.status_code == 429:
                time.sleep(8 * (attempt + 1))
                continue
        except Exception as e:
            last = str(e)
        time.sleep(2 * (attempt + 1))
    return key, "", last


def extract_ocr(source: str, pngs: dict, pages: list[tuple[int, int]],
                workers: int = 6) -> dict:
    model, prompt = OCR_MODELS[source], OCR_PROMPTS[source]
    api_key = os.getenv("SILICONFLOW_API_KEY")
    if not api_key:
        raise SystemExit("缺少 SILICONFLOW_API_KEY")
    cache_dir = CACHE / source
    cache_dir.mkdir(parents=True, exist_ok=True)
    out: dict[tuple[int, int], str] = {}
    todo = []
    for did, pg in pages:
        f = cache_dir / f"{did}_{pg}.json"
        if f.exists():
            out[(did, pg)] = json.loads(f.read_text(encoding="utf-8"))["text"]
        else:
            todo.append((did, pg))
    print(f"  [{source}] 缓存 {len(pages) - len(todo)} 页，待 OCR {len(todo)} 页")
    if not todo:
        return out
    tasks = [((did, pg), pngs[(did, pg)], model, prompt, api_key, 6) for did, pg in todo]
    done = errors = 0
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for key, text, err in ex.map(_ocr_one, tasks):
            done += 1
            if err:
                errors += 1
                print(f"  [{source}] {key} 失败: {err}")
            else:
                f = cache_dir / f"{key[0]}_{key[1]}.json"
                f.write_text(json.dumps({"text": text}, ensure_ascii=False), encoding="utf-8")
            out[key] = text
            if done % 25 == 0 or done == len(tasks):
                el = time.time() - t0
                print(f"  [{source}] {done}/{len(tasks)} 页，{el:.0f}s，"
                      f"{done / el:.1f} 页/秒，失败 {errors}")
    return out


# ---------------------------------------------------------------- 评测
_MD_STRIP = re.compile(r"[\s，。、,.！!？?；;：:\"'（）()【】\[\]《》<>%％|#*_`~]+")


def _norm_md(s: str) -> str:
    """在 docmap.norm_text 基础上额外剔除 markdown 表格/标题符号，保证各方案同口径。"""
    return _MD_STRIP.sub("", s)


def _hits(answer: str, text: str) -> bool:
    """answer_hits_page 的 markdown 兼容版：数字判定不变，文本判定用扩展归一化。"""
    from src.qa.docmap import _evidence_tokens, _lcs_len, _NEGATIVE_ANS, norm_answer
    tokens = _evidence_tokens(answer)
    if tokens:
        page_digits = re.sub(r"[^\d.％%\-]", "", text)
        return all(t in page_digits for t in tokens)
    if _NEGATIVE_ANS.search(answer):
        core = re.sub(r"^[一-鿿A-Za-z]{2,10}?公司", "", answer)
        core = re.sub(r"^(?:19|20)\d\d年", "", core)
        return _lcs_len(norm_answer(core), _norm_md(text)) >= 6
    return _lcs_len(norm_answer(answer), _norm_md(text)) >= 12


def evaluate(qs: list[dict], texts: dict, label: str) -> dict:
    q_hit = grp_hit = grp_total = 0
    failed_qs = []
    for q in qs:
        hit_any = False
        for g in q["groups"]:
            grp_total += 1
            hit = any(_hits(q["answer"], texts.get((q["doc_id"], p), "")) for p in g)
            grp_hit += 1 if hit else 0
            hit_any = hit_any or hit
        q_hit += 1 if hit_any else 0
        if not hit_any:
            failed_qs.append(q["qid"])
    n_chars = [len(t) for t in texts.values()]
    return {
        "label": label,
        "q_total": len(qs), "q_hit": q_hit,
        "q_rate": round(q_hit / len(qs) * 100, 1) if qs else None,
        "grp_total": grp_total, "grp_hit": grp_hit,
        "grp_rate": round(grp_hit / grp_total * 100, 1) if grp_total else None,
        "avg_chars": round(sum(n_chars) / len(n_chars)) if n_chars else 0,
        "empty_pages": sum(1 for t in texts.values() if not t.strip()),
        "failed_qs": failed_qs,
    }


# ---------------------------------------------------------------- 主流程
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只评测前 N 道题（小样本试跑）")
    ap.add_argument("--sources", default="pymupdf,pdfplumber,paddleocr,deepseek_ocr")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--report", default="")
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")
    conn = sqlite3.connect(ROOT / "db" / "finance.db")
    conn.row_factory = sqlite3.Row
    qs = load_questions(conn)
    if args.limit:
        qs = qs[: args.limit]
    pages = collect_pages(qs)
    paths = doc_paths(conn)
    print(f"评测题 {len(qs)} 道，涉及答案页 {len(pages)} 页")

    wanted = [s.strip() for s in args.sources.split(",") if s.strip()]
    results, texts_by_source = [], {}

    if "pymupdf" in wanted:
        print("[1/4] pymupdf（现有 pages 表）")
        texts_by_source["pymupdf"] = extract_pymupdf(conn, pages)
    if "pdfplumber" in wanted:
        print("[2/4] pdfplumber（规则提取）")
        texts_by_source["pdfplumber"] = extract_pdfplumber(paths, pages)
    ocr_sources = [s for s in wanted if s in OCR_MODELS]
    if ocr_sources:
        print("[3/4] 渲染页面 PNG")
        pngs = render_pngs(paths, pages)
        for i, src in enumerate(ocr_sources):
            print(f"[4/4] {src} OCR（{len(pages)} 页）")
            texts_by_source[src] = extract_ocr(src, pngs, pages, args.workers)

    for src, texts in texts_by_source.items():
        results.append(evaluate(qs, texts, src))

    # 报告
    lines = ["# PDF 解析方案对比报告", "",
             f"- 日期：{time.strftime('%Y-%m-%d %H:%M')}",
             f"- 评测集：CFQA 有效题 {len(qs)} 道（标注答案页 {len(pages)} 页）",
             "- 判定口径：答案证据数字/文本是否出现在**标注页码本身**"
             "（gate2 同口径，不含 ±2 窗口）",
             "", "| 方案 | 题级命中率 | 页码组命中率 | 平均字符/页 | 空页 |",
             "|---|---|---|---|---|"]
    for r in sorted(results, key=lambda x: -x["q_rate"]):
        lines.append(f"| {r['label']} | **{r['q_rate']}%**（{r['q_hit']}/{r['q_total']}） | "
                     f"{r['grp_rate']}%（{r['grp_hit']}/{r['grp_total']}） | "
                     f"{r['avg_chars']} | {r['empty_pages']} |")
    # 与 pymupdf 的逐题差异
    base = next((r for r in results if r["label"] == "pymupdf"), None)
    if base:
        base_set = set(base["failed_qs"])
        lines += ["", "## 相对 pymupdf 的逐题变化", "",
                  "| 方案 | 新增命中 | 丢失命中 |", "|---|---|---|"]
        for r in results:
            if r["label"] == "pymupdf":
                continue
            s = set(r["failed_qs"])
            lines.append(f"| {r['label']} | {len(base_set - s)} 题 | {len(s - base_set)} 题 |")
    out = args.report or str(ROOT / "reports" / f"parse_compare_{time.strftime('%Y%m%d_%H%M%S')}.md")
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"\n报告：{out}")
    conn.close()


if __name__ == "__main__":
    main()
