"""同一批题上对比 Agent 与 RAG 直答（同样的抽样与判分口径）。

用法：
    python -X utf8 scripts/agent_vs_rag.py                # 80 题对照
    python -X utf8 scripts/agent_vs_rag.py --limit 20 --skip-judge
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import paths  # noqa: E402
from src.agent.eval import _is_refusal, _judge_answer, _sample_questions  # noqa: E402
from src.agent.loop import agent_ask  # noqa: E402
from src.config import load  # noqa: E402
from src.rag.ask import ask as rag_ask  # noqa: E402


def _run(questions, fn, name: str, skip_judge: bool, workers: int = 6) -> dict:
    import concurrent.futures as cf
    import threading
    stats = {"name": name, "total": len(questions), "answered": 0, "correct": 0,
             "numeric_n": 0, "numeric_c": 0, "text_n": 0, "text_c": 0,
             "refused": 0, "refused_wrong": 0, "iters": 0}
    lock = threading.Lock()
    done = [0]

    def _one(q):
        try:
            r = fn(q["question"])
        except Exception as e:
            return q, None, str(e)
        return q, r, ""

    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for q, r, err in ex.map(_one, questions):
            with lock:
                done[0] += 1
                if err:
                    print(f"  [{name}] q{q['question_id']} 失败: {err}", flush=True)
                else:
                    pred = r.get("answer") or ""
                    if _is_refusal(pred):
                        stats["refused"] += 1
                    ok, kind = _judge_answer(q, pred, skip_judge)
                    if kind == "numeric":
                        stats["numeric_n"] += 1
                        stats["numeric_c"] += int(bool(ok))
                    elif kind == "text":
                        stats["text_n"] += 1
                        stats["text_c"] += int(bool(ok))
                    if ok is not None:
                        stats["answered"] += 1
                        stats["correct"] += int(bool(ok))
                        if ok is False and _is_refusal(pred):
                            stats["refused_wrong"] += 1
                    stats["iters"] += r.get("iterations", 1)
                if done[0] % 10 == 0:
                    print(f"  [{name}] {done[0]}/{len(questions)} 正确 "
                          f"{stats['correct']}/{stats['answered']} 拒答 {stats['refused']}",
                          flush=True)
    return stats


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--skip-judge", action="store_true")
    ap.add_argument("--per-company", type=int, default=8)
    args = ap.parse_args()
    cfg = load()
    conn = sqlite3.connect(paths.db_path())
    conn.row_factory = sqlite3.Row
    qs = _sample_questions(conn, cfg, per_company=args.per_company)
    if args.limit:
        qs = qs[: args.limit]
    conn.close()

    t0 = time.time()
    rag = _run(qs, lambda q: rag_ask(q, cfg), "RAG", args.skip_judge)
    print(f"RAG 用时 {time.time() - t0:.0f}s")
    t0 = time.time()
    ag = _run(qs, lambda q: agent_ask(q, cfg), "Agent", args.skip_judge)
    print(f"Agent 用时 {time.time() - t0:.0f}s")

    def pct(a, b):
        return round(a / b * 100, 1) if b else None

    out = {"questions": len(qs), "rag": rag, "agent": ag,
           "rag_acc": pct(rag["correct"], rag["answered"]),
           "agent_acc": pct(ag["correct"], ag["answered"]),
           "rag_refuse": pct(rag["refused"], rag["total"]),
           "agent_refuse": pct(ag["refused"], ag["total"])}
    ts = time.strftime("%Y%m%d_%H%M%S")
    report = paths.dir_reports() / f"agent_vs_rag_{ts}.md"
    lines = [f"# Agent vs RAG 对照 {time.strftime('%Y-%m-%d %H:%M')}", "",
             f"- 同一批 {len(qs)} 题（seed 42，每公司 {args.per_company} 题）",
             f"- 判分口径相同：数值题自动（1% 容差，含单位换算）/ 文本题 LLM judge", "",
             "| 指标 | RAG 直答 | Agent |", "|---|---|---|",
             f"| 答案准确率 | {out['rag_acc']}% ({rag['correct']}/{rag['answered']}) | "
             f"{out['agent_acc']}% ({ag['correct']}/{ag['answered']}) |",
             f"| 数值题 | {pct(rag['numeric_c'], rag['numeric_n'])}% ({rag['numeric_n']}) | "
             f"{pct(ag['numeric_c'], ag['numeric_n'])}% ({ag['numeric_n']}) |",
             f"| 文本题 | {pct(rag['text_c'], rag['text_n'])}% ({rag['text_n']}) | "
             f"{pct(ag['text_c'], ag['text_n'])}% ({ag['text_n']}) |",
             f"| 拒答/未披露 | {rag['refused']} | {ag['refused']} |",
             f"| 其中「拒答但 gold 有答案」 | {rag['refused_wrong']} | {ag['refused_wrong']} |",
             f"| 平均迭代轮数 | {round(rag['iters'] / len(qs), 2)} | "
             f"{round(ag['iters'] / len(qs), 2)} |"]
    report.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"\n报告：{report}")


if __name__ == "__main__":
    main()
