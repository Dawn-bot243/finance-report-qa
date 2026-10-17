"""CLI 问答入口：提问 → 结构化直答 → 混合检索 → 生成带出处回答。"""
from __future__ import annotations

import sqlite3

from .. import paths
from ..config import Config, load
from ..logutil import setup_logging
from .generator import generate
from .retriever import analyze_query, chunk_citation, retrieve
from .structured import structured_answer

log = setup_logging(__name__)


def ask(question: str, cfg: Config | None = None, top_k: int = 5, verbose: bool = False) -> dict:
    cfg = cfg or load()
    conn = sqlite3.connect(paths.db_path())
    conn.row_factory = sqlite3.Row

    info = analyze_query(question, cfg)
    result = {"question": question, "path": None, "answer": None, "citations": [], "info": {
        "code": info.code, "year": info.year, "metric_keys": info.metric_keys}}

    # ① 结构化直答
    s = structured_answer(conn, info, question)
    if s:
        result["path"] = "structured"
        result["answer"] = s
        result["citations"] = re_find_citations(s)
        conn.close()
        return result

    # ② 混合检索 + 生成
    chunks = retrieve(question, cfg, top_k=top_k)
    if verbose:
        for c in chunks:
            print(f"  [{c.origin} {c.score:.3f}] doc{c.doc_id} p{c.page_no} {c.code} FY{c.fiscal_year} | {c.text[:60]}")
    gen = generate(question, chunks)
    result["path"] = "rag"
    result["answer"] = gen["answer"]
    result["citations"] = gen["citations"]
    result["unchecked_numbers"] = gen["unchecked_numbers"]
    result["chunks"] = [{"doc_id": c.doc_id, "page_no": c.page_no, "score": round(c.score, 4)}
                        for c in chunks]
    conn.close()
    return result


def re_find_citations(text: str) -> list[str]:
    import re
    return re.findall(r"【[^】]*】", text)


def print_answer(result: dict) -> None:
    print(f"\n问题：{result['question']}")
    print(f"路径：{result['path']}")
    print(f"\n回答：\n{result['answer']}")
    if result.get("citations"):
        print("\n引用：")
        for c in result["citations"]:
            print(f"  - {c}")
