"""docmap：CFQA 问题 → 文档映射。按答案文本在页码组中的命中率选最优文档（招股书多候选定主）。"""
from __future__ import annotations

import json
import re
import sqlite3

from ..config import Config
from ..logutil import setup_logging

log = setup_logging(__name__)

_STRIP = re.compile(r"[\s，。、,.！!？?；;：:\"'（）()【】\[\]《》<>%％]+")


def norm_answer(s: str) -> str:
    """答案归一化：去空白标点、去「XXX公司」前缀。"""
    s = s.strip()
    s = re.sub(r"^[一-鿿A-Za-z]{2,10}?公司", "", s)
    return _STRIP.sub("", s)


def norm_text(s: str) -> str:
    return _STRIP.sub("", s)


_NUM_TOKEN = re.compile(r"[-−－]?[\d,，]{2,}(?:\.\d+)?%?")


def _lcs_len(a: str, b: str) -> int:
    """最长公共子串长度（O(n*m)，答案几百字×页面几千字，可接受）。"""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for ca in a:
        cur = [0] * (len(b) + 1)
        for j, cb in enumerate(b, 1):
            if ca == cb:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def answer_hits_page(answer: str, page_text: str) -> bool:
    """答案命中判定：
    - 含数字的答案（财报问答主流）：答案中所有数字 token 需出现在页面（千分位逗号、括号负数容差）
    - 纯文本答案：最长公共子串 ≥ 12 字
    """
    tokens = _NUM_TOKEN.findall(answer)
    if tokens:
        page_digits = re.sub(r"[^\d.％%\-]", "", page_text)
        for t in tokens:
            t = t.replace(",", "").replace("，", "").lstrip("－−")
            if t not in page_digits:
                return False
        return True
    return _lcs_len(norm_answer(answer), norm_text(page_text)) >= 12


def map_questions(conn: sqlite3.Connection, cfg: Config) -> dict:
    """全量映射，写回 cfqa_questions.qa_doc_map/qa_check。返回统计。"""
    stats = {"ok": 0, "partial": 0, "fail": 0, "skip": 0}
    qs = conn.execute(
        "SELECT * FROM cfqa_questions WHERE qa_check IS NULL OR qa_check=''"
    ).fetchall()
    docs_cache: dict[int, list[dict]] = {}
    for q in qs:
        pages_groups = json.loads(q["answer_pages"])
        if not pages_groups or all(not g for g in pages_groups):
            conn.execute("UPDATE cfqa_questions SET qa_check='skip' WHERE question_id=?",
                         (q["question_id"],))
            stats["skip"] += 1
            continue
        # 候选文档：同年份（无年份则该公司全部）
        cands = conn.execute(
            """SELECT d.doc_id, d.page_count, d.fiscal_year, d.doc_type, d.is_primary
               FROM documents d WHERE d.code=? AND (? IS NULL OR d.fiscal_year=?)
               AND d.page_count IS NOT NULL ORDER BY d.is_primary DESC, d.doc_id""",
            (q["code"], q["q_year"], q["q_year"]),
        ).fetchall()
        if not cands:
            conn.execute("UPDATE cfqa_questions SET qa_check='fail' WHERE question_id=?",
                         (q["question_id"],))
            stats["fail"] += 1
            continue
        ans = q["answer"]
        best: dict | None = None
        best_hits, best_score = -1, -1.0
        for d in cands:
            pages = docs_cache.setdefault(d["doc_id"], conn.execute(
                "SELECT page_no, text FROM pages WHERE doc_id=?", (d["doc_id"],)
            ).fetchall())
            text_by_no = {p["page_no"]: p["text"] for p in pages}
            groups = []
            hits = 0
            usable = 0
            for g in pages_groups:
                g = [pg for pg in g if pg <= d["page_count"]]
                if not g:
                    groups.append({"pages": g, "hit": False})
                    continue
                usable += 1
                hit = any(answer_hits_page(ans, text_by_no.get(pg, "")) for pg in g)
                if hit:
                    hits += 1
                groups.append({"pages": g, "hit": hit})
            score = hits / usable if usable else 0.0
            if hits > best_hits or (hits == best_hits and score > best_score):
                best_hits, best_score, best = hits, score, d
                best_groups = groups
        # 同 (code,fiscal_year) 多候选：命中数最多的文档
        status = "ok" if best_hits == len([g for g in best_groups if g["pages"]]) else (
            "partial" if best_hits > 0 else "fail")
        conn.execute(
            "UPDATE cfqa_questions SET qa_doc_map=?, qa_check=? WHERE question_id=?",
            (json.dumps({"doc_id": best["doc_id"], "fiscal_year": best["fiscal_year"],
                         "doc_type": best["doc_type"], "groups": best_groups},
                        ensure_ascii=False),
             status, q["question_id"]),
        )
        stats[status] += 1
    conn.commit()
    log.info("docmap 完成: %s", stats)
    return stats


def set_primary_by_hitrate(conn: sqlite3.Connection, cfg: Config) -> None:
    """同 (code, fiscal_year) 多候选文档：按该年问题命中率选 is_primary（招股书定主）。"""
    groups = conn.execute(
        "SELECT code, fiscal_year FROM documents GROUP BY code, fiscal_year HAVING COUNT(*)>1"
    ).fetchall()
    for g in groups:
        docs = conn.execute(
            "SELECT doc_id FROM documents WHERE code=? AND fiscal_year=?",
            (g["code"], g["fiscal_year"]),
        ).fetchall()
        rates = {}
        for d in docs:
            qs = conn.execute(
                """SELECT qa_check FROM cfqa_questions
                   WHERE code=? AND q_year=? AND qa_doc_map LIKE ?""",
                (g["code"], g["fiscal_year"], f'%"doc_id": {d["doc_id"]}%'),
            ).fetchall()
            hit = sum(1 for r in qs if r["qa_check"] in ("ok", "partial"))
            rates[d["doc_id"]] = hit
        best_id = max(rates, key=rates.get)
        conn.execute("UPDATE documents SET is_primary=0 WHERE code=? AND fiscal_year=?",
                     (g["code"], g["fiscal_year"]))
        conn.execute("UPDATE documents SET is_primary=1 WHERE doc_id=?", (best_id,))
        log.info("%s FY%d 多候选定主: doc %d (%s)", g["code"], g["fiscal_year"], best_id, rates)
    conn.commit()
