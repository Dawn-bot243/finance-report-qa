"""自校正检索对照评估：Agent（search_report 自校正）vs Agent（单次检索）。

同一批 CFQA 抽样题、同一判分口径，只切换 search_report 是否启用
「相关性评估 → 查询重写 → 重新检索」。

用法：
    python -X utf8 scripts/corrective_eval.py                 # 40 题（每公司 4 题）
    python -X utf8 scripts/corrective_eval.py --limit 12 --skip-judge
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


def _run(questions, cfg, corrective: bool, skip_judge: bool, workers: int = 6) -> dict:
    stats = {"corrective": corrective, "total": len(questions), "answered": 0, "correct": 0,
             "numeric_n": 0, "numeric_c": 0, "text_n": 0, "text_c": 0,
             "refused": 0, "rewrites": 0, "tool_calls": 0, "elapsed": 0.0}
    lock = threading.Lock()
    done = [0]

    def _one(q):
        t0 = time.time()
        try:
            r = agent_ask(q["question"], cfg, corrective=corrective)
            return q, r, time.time() - t0, ""
        except Exception as e:
            return q, None, time.time() - t0, str(e)

    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for q, r, el, err in ex.map(_one, questions):
            with lock:
                done[0] += 1
                stats["elapsed"] += el
                if err:
                    print(f"  [{'corrective' if corrective else 'single'}] "
                          f"q{q['question_id']} 失败: {err}", flush=True)
                    continue
                pred = r["answer"] or ""
                if _is_refusal(pred):
                    stats["refused"] += 1
                stats["tool_calls"] += len(r["trace"])
                # 统计检索重写：工具结果里带「检索说明」或「证据不足」说明触发了评估/重写
                stats["rewrites"] += sum(1 for t in r["trace"]
                                         if "检索说明" in t.get("result_preview", ""))
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
                if done[0] % 10 == 0:
                    print(f"  [{'corrective' if corrective else 'single'}] {done[0]}/"
                          f"{len(questions)} 正确 {stats['correct']}/{stats['answered']} "
                          f"拒答 {stats['refused']}", flush=True)
    return stats


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--skip-judge", action="store_true")
    ap.add_argument("--per-company", type=int, default=4)
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()
    cfg = load()
    conn = sqlite3.connect(paths.db_path())
    conn.row_factory = sqlite3.Row
    qs = _sample_questions(conn, cfg, per_company=args.per_company)
    conn.close()
    if args.limit:
        qs = qs[: args.limit]

    t0 = time.time()
    single = _run(qs, cfg, False, args.skip_judge, args.workers)
    print(f"单次检索组用时 {time.time() - t0:.0f}s")
    t0 = time.time()
    corr = _run(qs, cfg, True, args.skip_judge, args.workers)
    print(f"自校正组用时 {time.time() - t0:.0f}s")

    def pct(a, b):
        return round(a / b * 100, 1) if b else None

    ts = time.strftime("%Y%m%d_%H%M%S")
    report = paths.dir_reports() / f"corrective_eval_{ts}.md"
    lines = [f"# 自校正检索对照评估 {time.strftime('%Y-%m-%d %H:%M')}", "",
             f"- 同一批 {len(qs)} 题（seed 42，每公司 {args.per_company} 题）",
             "- 仅切换 search_report 是否启用「评估 → 重写 → 重检索」",
             "- 判分：数值题自动（1% 容差 + 单位换算）/ 文本题 LLM judge", "",
             "| 指标 | 单次检索 | **自校正检索** |", "|---|---|---|",
             f"| 答案准确率 | {pct(single['correct'], single['answered'])}% "
             f"({single['correct']}/{single['answered']}) | "
             f"**{pct(corr['correct'], corr['answered'])}%** "
             f"({corr['correct']}/{corr['answered']}) |",
             f"| 数值题 | {pct(single['numeric_c'], single['numeric_n'])}% "
             f"({single['numeric_n']}) | {pct(corr['numeric_c'], corr['numeric_n'])}% "
             f"({corr['numeric_n']}) |",
             f"| 文本题 | {pct(single['text_c'], single['text_n'])}% ({single['text_n']}) | "
             f"{pct(corr['text_c'], corr['text_n'])}% ({corr['text_n']}) |",
             f"| 拒答/未披露 | {single['refused']} | {corr['refused']} |",
             f"| 触发重写的工具调用 | {single['rewrites']} | {corr['rewrites']} |",
             f"| 工具调用总数 | {single['tool_calls']} | {corr['tool_calls']} |",
             f"| 总耗时 | {single['elapsed']:.0f}s | {corr['elapsed']:.0f}s |"]
    report.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"\n报告：{report}")


if __name__ == "__main__":
    main()
