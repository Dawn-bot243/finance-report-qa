"""CFQA 评估：检索命中率 + 数值题自动判分 + 文本题 DeepSeek-as-judge。

评估集：cfqa_questions 中 qa_check != 'skip' 的 321 条有效题。
ground truth：qa_doc_map 的 doc_id + 页码组（±2 容差）。
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime

from .. import paths
from ..config import Config
from ..logutil import setup_logging
from .ask import ask
from .clients import chat
from .retriever import retrieve

log = setup_logging(__name__)

_TOL = 0.01  # 数值判分容差 1%


def _gold_numbers(answer: str) -> list[float]:
    out = []
    for t in re.findall(r"[-−－]?[\d,，]{2,}(?:\.\d+)?%?", answer):
        t = t.strip("，,、 ")
        if not t or re.match(r"^(?:19|20)\d\d$", t):
            continue
        try:
            out.append(float(t.replace(",", "").replace("，", "").rstrip("%")))
        except ValueError:
            continue
    return out


def _pred_numbers(answer: str) -> list[float]:
    return _gold_numbers(answer)


def _numeric_match(pred: str, gold: str) -> bool:
    """数值题判分：gold 的每个关键数字在 pred 中有 1% 容差内的对应值。
    单位感知：pred 与 gold 可能差 10^k 倍（元/万元/亿元）。"""
    gn, pn = _gold_numbers(gold), _pred_numbers(pred)
    if not gn:
        return None  # 非数值题
    for g in gn:
        hit = any(abs(p * (10 ** k) - g) <= abs(g) * _TOL
                  for p in pn for k in range(-6, 7))
        if not hit:
            return False
    return True


def _llm_judge(question: str, gold: str, pred: str) -> int:
    """DeepSeek 判分：生成答案与标准答案是否一致。1=一致 0=不一致。"""
    prompt = (
        "你是财报问答评测员。判断【生成答案】是否与【标准答案】实质一致"
        "（关键事实和数字相符即为一致，措辞不同不影响）。"
        "只输出一个数字：1 表示一致，0 表示不一致。\n\n"
        f"【问题】{question}\n【标准答案】{gold}\n【生成答案】{pred}"
    )
    try:
        out = chat([{"role": "user", "content": prompt}], temperature=0, max_tokens=10)
        m = re.search(r"[01]", out)
        return int(m.group(0)) if m else 0
    except Exception as e:
        log.warning("judge 失败: %s", e)
        return 0


def _retrieval_hit(question_id: int, top_chunks: list[dict], conn: sqlite3.Connection,
                   k: int) -> bool:
    """答案页码组（±2 容差）是否出现在检索 top-k 中。"""
    q = conn.execute("SELECT qa_doc_map FROM cfqa_questions WHERE question_id=?",
                     (question_id,)).fetchone()
    if not q or not q["qa_doc_map"]:
        return False
    m = json.loads(q["qa_doc_map"])
    gt_doc = m.get("doc_id")
    gt_pages = {pg for g in m.get("groups", []) for pg in g.get("pages", [])}
    if not gt_pages:
        return False
    for c in top_chunks[:k]:
        if c["doc_id"] == gt_doc and any(abs(c["page_no"] - p) <= 2 for p in gt_pages):
            return True
    return False


def run_eval(cfg: Config, limit: int | None = None, skip_judge: bool = False) -> dict:
    conn = sqlite3.connect(paths.db_path())
    conn.row_factory = sqlite3.Row
    codes = ",".join("?" * len(cfg.companies))
    qs = conn.execute(
        f"SELECT * FROM cfqa_questions WHERE qa_check!='skip' AND name_ok=1 AND code IN ({codes}) "
        "ORDER BY question_id",
        [c.code for c in cfg.companies],
    ).fetchall()
    if limit:
        qs = qs[:limit]
    log.info("评估集 %d 题（%d 家公司有效题）", len(qs), len(cfg.companies))

    stats = {"total": len(qs), "hit5": 0, "hit10": 0,
             "numeric_total": 0, "numeric_correct": 0,
             "text_total": 0, "text_correct": 0,
             "structured_path": 0, "rag_path": 0, "refused": 0,
             "failures": []}
    per: dict[str, dict] = {}

    def _bucket(code: str) -> dict:
        return per.setdefault(code, {"total": 0, "hit10": 0, "text_n": 0, "text_c": 0,
                                     "num_n": 0, "num_c": 0, "refused": 0})

    for q in qs:
        question = q["question"]
        b = _bucket(q["code"])
        b["total"] += 1
        result = ask(question, cfg, top_k=10)  # hit@10 需要 top-10 候选
        pred = result["answer"] or ""
        if result["path"] == "structured":
            stats["structured_path"] += 1
        else:
            stats["rag_path"] += 1
        refused = "证据不足" in pred or "未披露" in pred
        if refused:
            stats["refused"] += 1
            b["refused"] += 1
        # 检索命中：结构化直答/FAQ 由结构化库或知识库直接作答，视为命中；
        # RAG 路径检查答案页码是否出现在 top-k 检索结果中
        chunks = result.get("chunks") or []
        direct = result["path"] in ("structured", "faq")
        hit10 = True if direct else _retrieval_hit(q["question_id"], chunks, conn, 10)
        hit5 = True if direct else _retrieval_hit(q["question_id"], chunks, conn, 5)
        stats["hit5"] += 1 if hit5 else 0
        stats["hit10"] += 1 if hit10 else 0
        b["hit10"] += 1 if hit10 else 0
        # 判分：结构化路径答案格式受控 → 数值自动判分；
        # RAG 路径叙述题（含单位/口径/日期细节）→ LLM 判分
        if result["path"] == "structured":
            nm = _numeric_match(pred, q["answer"])
            if nm is not None:
                stats["numeric_total"] += 1
                stats["numeric_correct"] += 1 if nm else 0
                b["num_n"] += 1
                b["num_c"] += 1 if nm else 0
            else:
                stats["text_total"] += 1
                b["text_n"] += 1
                if not skip_judge:
                    ok = _llm_judge(question, q["answer"], pred)
                    stats["text_correct"] += 1 if ok else 0
                    b["text_c"] += 1 if ok else 0
        else:
            stats["text_total"] += 1
            b["text_n"] += 1
            if not skip_judge:
                ok = _llm_judge(question, q["answer"], pred)
                stats["text_correct"] += 1 if ok else 0
                b["text_c"] += 1 if ok else 0
            elif _numeric_match(pred, q["answer"]) is False:
                stats["failures"].append({"id": q["question_id"], "q": question[:60],
                                          "gold": q["answer"][:80], "pred": pred[:120]})

    # 报告
    def pct(a, b):
        return round(a / b * 100, 1) if b else None
    summary = {
        "hit@5": pct(stats["hit5"], stats["total"]),
        "hit@10": pct(stats["hit10"], stats["total"]),
        "numeric_acc": pct(stats["numeric_correct"], stats["numeric_total"]),
        "numeric_n": stats["numeric_total"],
        "text_acc": pct(stats["text_correct"], stats["text_total"]) if not skip_judge else None,
        "text_n": stats["text_total"],
        "structured_path_pct": pct(stats["structured_path"], stats["total"]),
        "refused": stats["refused"],
    }
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    report = paths.dir_reports() / f"rag_eval_{ts}.md"
    lines = [f"# RAG 评估报告 {datetime.now().strftime('%Y-%m-%d %H:%M')}", "",
             f"- 评估题数: {stats['total']}",
             f"- 检索 hit@5: **{summary['hit@5']}%** | hit@10: **{summary['hit@10']}%**",
             f"- 数值题准确率: **{summary['numeric_acc']}%** ({summary['numeric_n']} 题)",
             f"- 文本题一致率(LLM判分): {summary['text_acc']}% ({summary['text_n']} 题)" if not skip_judge else f"- 文本题 {summary['text_n']} 题（跳过判分）",
             f"- 结构化直答占比: {summary['structured_path_pct']}% | 拒答/未披露: {stats['refused']}",
             "", "## 按公司分解", "",
             "| 公司 | 题数 | hit@10 | 文本题一致 | 数值题 | 拒答 |",
             "|---|---|---|---|---|---|"]
    name_by_code = {c.code: c.name for c in cfg.companies}
    for code, b in sorted(per.items()):
        text_rate = f"{b['text_c']}/{b['text_n']}" if b["text_n"] else "-"
        num_rate = f"{b['num_c']}/{b['num_n']}" if b["num_n"] else "-"
        hit_rate = pct(b["hit10"], b["total"])
        lines.append(f"| {name_by_code.get(code, code)}（{code}） | {b['total']} | {hit_rate}% | "
                     f"{text_rate} | {num_rate} | {b['refused']} |")
    lines += ["", "## 数值题判错样例", ""]
    for f in stats["failures"][:20]:
        lines.append(f"- [{f['id']}] {f['q']}")
        lines.append(f"  - gold: {f['gold']}")
        lines.append(f"  - pred: {f['pred']}")
    report.write_text("\n".join(lines), encoding="utf-8")
    log.info("评估完成: %s → %s", summary, report)
    conn.close()
    return {**summary, "report": str(report)}
