"""端到端对照：Agent（多查询开/关），同一批题、多轮判分。

用法：
    python -X utf8 scripts/agent_mq_eval.py                 # 40 题（每公司 4 题）
    python -X utf8 scripts/agent_mq_eval.py --limit 12 --skip-judge
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import sqlite3
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import paths  # noqa: E402
from src.agent.eval import _is_refusal, _judge_answer, _sample_questions  # noqa: E402
from src.agent.loop import agent_ask  # noqa: E402
from src.config import load  # noqa: E402


def _run(qs, cfg, multi_query: bool, skip_judge: bool, judge_rounds: int,
         workers: int) -> dict:
    stats = {"mq": multi_query, "total": len(qs), "answered": 0, "correct": 0,
             "numeric_n": 0, "numeric_c": 0, "text_n": 0, "text_c": 0,
             "refused": 0, "elapsed": 0.0}
    lock = threading.Lock()
    done = [0]

    def _one(q):
        t0 = time.time()
        try:
            r = agent_ask(q["question"], cfg, multi_query=multi_query)
            return q, r, time.time() - t0, ""
        except Exception as e:
            return q, None, time.time() - t0, str(e)

    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for q, r, el, err in ex.map(_one, qs):
            with lock:
                done[0] += 1
                stats["elapsed"] += el
                if err:
                    print(f"  [{'MQ' if multi_query else 'SQ'}] q{q['question_id']} 失败: {err}",
                          flush=True)
                    continue
                pred = r["answer"] or ""
                if _is_refusal(pred):
                    stats["refused"] += 1
                ok, kind = _judge_answer(q, pred, skip_judge, judge_rounds=judge_rounds)
                if kind == "numeric":
                    stats["numeric_n"] += 1
                    stats["numeric_c"] += int(bool(ok))
                elif kind == "text":
                    stats["text_n"] += 1
                    stats["text_c"] += int(bool(ok))
                if ok is not None:
                    stats["answered"] += 1
                    stats["correct"] += int(bool(ok))
                if done[0] % 10 == 0:
                    print(f"  [{'MQ' if multi_query else 'SQ'}] {done[0]}/{len(qs)} "
                          f"正确 {stats['correct']}/{stats['answered']} "
                          f"拒答 {stats['refused']}", flush=True)
    return stats


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--skip-judge", action="store_true")
    ap.add_argument("--judge-rounds", type=int, default=3)
    ap.add_argument("--per-company", type=int, default=4)
    ap.add_argument("--workers", type=int, default=5)
    args = ap.parse_args()
    cfg = load()
    conn = sqlite3.connect(paths.db_path())
    conn.row_factory = sqlite3.Row
    qs = _sample_questions(conn, cfg, per_company=args.per_company)
    conn.close()
    if args.limit:
        qs = qs[: args.limit]
    print(f"题数 {len(qs)}；判分 {args.judge_rounds} 轮")

    t0 = time.time()
    sq = _run(qs, cfg, False, args.skip_judge, args.judge_rounds, args.workers)
    print(f"单查询用时 {time.time() - t0:.0f}s")
    t0 = time.time()
    mq = _run(qs, cfg, True, args.skip_judge, args.judge_rounds, args.workers)
    print(f"多查询用时 {time.time() - t0:.0f}s")

    def pct(a, b):
        return round(a / b * 100, 1) if b else None

    ts = time.strftime("%Y%m%d_%H%M%S")
    report = paths.dir_reports() / f"agent_mq_eval_{ts}.md"
    lines = [f"# Agent 多查询端到端对照 {time.strftime('%Y-%m-%d %H:%M')}", "",
             f"- 同一批 {len(qs)} 题（seed 42，每公司 {args.per_company} 题）",
             f"- 判分：数值题自动 / 文本题 LLM {args.judge_rounds} 轮取多数", "",
             "| 指标 | 单查询 | **多查询** |", "|---|---|---|",
             f"| 答案准确率 | {pct(sq['correct'], sq['answered'])}% "
             f"({sq['correct']}/{sq['answered']}) | "
             f"**{pct(mq['correct'], mq['answered'])}%** "
             f"({mq['correct']}/{mq['answered']}) |",
             f"| 数值题 | {pct(sq['numeric_c'], sq['numeric_n'])}% ({sq['numeric_n']}) | "
             f"{pct(mq['numeric_c'], mq['numeric_n'])}% ({mq['numeric_n']}) |",
             f"| 文本题 | {pct(sq['text_c'], sq['text_n'])}% ({sq['text_n']}) | "
             f"{pct(mq['text_c'], mq['text_n'])}% ({mq['text_n']}) |",
             f"| 拒答 | {sq['refused']} | {mq['refused']} |",
             f"| 总耗时 | {sq['elapsed']:.0f}s | {mq['elapsed']:.0f}s |"]
    report.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"\n报告：{report}")


if __name__ == "__main__":
    main()
