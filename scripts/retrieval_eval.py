"""检索层评估：只跑 retrieve（不生成、不判分），对比查询策略的命中率。

用于快速验证「语义切分 / 多查询检索」对召回的影响，成本远低于端到端评估。

用法：
    python -X utf8 scripts/retrieval_eval.py                 # 全量 543 题，单查询 vs 多查询
    python -X utf8 scripts/retrieval_eval.py --limit 100     # 抽样
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import sqlite3
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import paths  # noqa: E402
from src.config import load  # noqa: E402
from src.rag.retriever import generate_queries, retrieve  # noqa: E402


def _load_questions(conn, cfg, limit: int | None) -> list[dict]:
    codes = ",".join("?" * len(cfg.companies))
    rows = conn.execute(
        f"SELECT question_id, code, question, answer, qa_doc_map FROM cfqa_questions "
        f"WHERE qa_check!='skip' AND name_ok=1 AND code IN ({codes}) "
        "ORDER BY question_id", [c.code for c in cfg.companies]).fetchall()
    qs = []
    for r in rows:
        m = json.loads(r["qa_doc_map"])
        pages = {p for g in m.get("groups", []) for p in g.get("pages", [])}
        if not pages:
            continue
        qs.append({"id": r["question_id"], "code": r["code"], "q": r["question"],
                   "doc_id": m["doc_id"], "pages": pages})
    return qs[:limit] if limit else qs


def _run(qs, cfg, multi_query: bool, top_k: int, workers: int) -> dict:
    stats = {"total": len(qs), "hit5": 0, "hit10": 0, "mrr": 0.0,
             "query_calls": 0, "elapsed": 0.0}
    lock = threading.Lock()
    done = [0]

    def _one(item):
        t0 = time.time()
        queries = generate_queries(item["q"], n=3) if multi_query else None
        chunks = retrieve(item["q"], cfg, top_k=top_k, queries=queries)
        return item, chunks, len(queries or []), time.time() - t0

    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for item, chunks, nq, el in ex.map(_one, qs):
            with lock:
                stats["elapsed"] += el
                stats["query_calls"] += nq
                ranks = [i + 1 for i, c in enumerate(chunks)
                         if c.doc_id == item["doc_id"]
                         and any(abs(c.page_no - p) <= 2 for p in item["pages"])]
                if ranks:
                    stats["mrr"] += 1.0 / ranks[0]
                    stats["hit10"] += int(ranks[0] <= 10)
                    stats["hit5"] += int(ranks[0] <= 5)
                done[0] += 1
                if done[0] % 50 == 0:
                    print(f"  [{ 'MQ' if multi_query else 'SQ'}] {done[0]}/{len(qs)} "
                          f"hit@10={stats['hit10'] / done[0] * 100:.1f}%", flush=True)
    return stats


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--top-k", type=int, default=10)
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()
    cfg = load()
    conn = sqlite3.connect(paths.db_path())
    conn.row_factory = sqlite3.Row
    qs = _load_questions(conn, cfg, args.limit or None)
    conn.close()
    print(f"评估题数: {len(qs)}")

    t0 = time.time()
    sq = _run(qs, cfg, False, args.top_k, args.workers)
    print(f"单查询用时 {time.time() - t0:.0f}s")
    t0 = time.time()
    mq = _run(qs, cfg, True, args.top_k, args.workers)
    print(f"多查询用时 {time.time() - t0:.0f}s")

    def pct(a, b):
        return round(a / b * 100, 1) if b else None

    ts = time.strftime("%Y%m%d_%H%M%S")
    report = paths.dir_reports() / f"retrieval_eval_{ts}.md"
    lines = [f"# 检索层评估 {time.strftime('%Y-%m-%d %H:%M')}", "",
             f"- 题数：{len(qs)}（CFQA 有效题，含答案页码）",
             f"- 索引：语义子块（36,583 块）；命中判定：答案页 ±2 在 top-k 内", "",
             "| 指标 | 单查询 | **多查询（3 改写）** |", "|---|---|---|",
             f"| hit@5 | {pct(sq['hit5'], sq['total'])}% | "
             f"**{pct(mq['hit5'], mq['total'])}%** |",
             f"| hit@10 | {pct(sq['hit10'], sq['total'])}% | "
             f"**{pct(mq['hit10'], mq['total'])}%** |",
             f"| MRR | {round(sq['mrr'] / sq['total'], 3)} | "
             f"{round(mq['mrr'] / mq['total'], 3)} |",
             f"| 平均耗时/题 | {round(sq['elapsed'] / sq['total'], 2)}s | "
             f"{round(mq['elapsed'] / mq['total'], 2)}s |"]
    report.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"\n报告：{report}")


if __name__ == "__main__":
    main()
