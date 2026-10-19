"""Agent 评估：CFQA 分层抽样 + 手写多步任务。

指标：
- 路由正确率：期望工具是否出现在 tool trace（期望由现有 ask() 路径离线预标）
- 答案判分：数值题自动（容差 1%）/ 文本题 DeepSeek judge（复用 rag.eval）
- 拒答率、平均迭代轮数、工具调用失败率
"""
from __future__ import annotations

import json
import random
import re
import sqlite3
import time
from datetime import datetime

from .. import paths
from ..config import Config
from ..logutil import setup_logging
from ..rag.ask import ask as rag_ask
from ..rag.eval import _llm_judge, _numeric_match
from .loop import agent_ask
from .tasks import MULTI_STEP_TASKS

log = setup_logging(__name__)

# ask() 路径 → Agent 期望工具
_PATH_TO_TOOL = {"structured": "get_metric", "faq": "explain_metric",
                 "rag": "search_report"}


def _sample_questions(conn: sqlite3.Connection, cfg: Config,
                      per_company: int = 8, seed: int = 42) -> list[dict]:
    rng = random.Random(seed)
    out = []
    for c in cfg.companies:
        rows = conn.execute(
            "SELECT * FROM cfqa_questions WHERE qa_check!='skip' AND name_ok=1 AND code=? "
            "ORDER BY question_id", (c.code,)).fetchall()
        out.extend(rng.sample(rows, min(per_company, len(rows))))
    return out


def _expected_tools(questions: list[dict], cfg: Config) -> dict[int, str]:
    """用现有 ask() 的路径离线预标期望工具（faq/structured/rag）。"""
    expected: dict[int, str] = {}
    for i, q in enumerate(questions, 1):
        try:
            r = rag_ask(q["question"], cfg)
            expected[q["question_id"]] = _PATH_TO_TOOL.get(r["path"], "search_report")
        except Exception as e:
            log.warning("预标失败 q%d: %s", q["question_id"], e)
            expected[q["question_id"]] = "search_report"
        if i % 10 == 0:
            print(f"  预标 {i}/{len(questions)}")
    return expected


def _is_refusal(text: str) -> bool:
    return "证据不足" in text or "未披露" in text


def _judge_answer(q: dict, pred: str, skip_judge: bool) -> tuple[bool | None, str]:
    """返回 (是否正确, 判分类型)。数值题自动判分，文本题 LLM 判分。"""
    nm = _numeric_match(pred, q["answer"])
    if nm is not None:
        return nm, "numeric"
    if skip_judge:
        return None, "text_skipped"
    return bool(_llm_judge(q["question"], q["answer"], pred)), "text"


def _tool_stats(trace: list[dict]) -> dict:
    return {"calls": len(trace),
            "failed": sum(1 for t in trace if not t["ok"]),
            "tools": [t["tool"] for t in trace]}


def run_agent_eval(cfg: Config, limit: int | None = None, skip_judge: bool = False,
                   max_iters: int = 6, per_company: int = 8) -> dict:
    conn = sqlite3.connect(paths.db_path())
    conn.row_factory = sqlite3.Row
    qs = _sample_questions(conn, cfg, per_company=per_company)
    if limit:
        qs = qs[:limit]
    print(f"CFQA 抽样 {len(qs)} 题；预标期望工具…")
    t0 = time.time()
    expected = _expected_tools(qs, cfg)
    print(f"预标完成，用时 {time.time() - t0:.0f}s；开始 Agent 评估…")

    stats = {"total": len(qs), "route_hit": 0, "answer_total": 0, "answer_correct": 0,
             "numeric_total": 0, "numeric_correct": 0, "text_total": 0, "text_correct": 0,
             "refused": 0, "iters": 0, "tool_calls": 0, "tool_failed": 0,
             "failures": []}
    per_company_stats: dict[str, dict] = {}

    for i, q in enumerate(qs, 1):
        b = per_company_stats.setdefault(q["code"], {"total": 0, "route_hit": 0,
                                                     "correct": 0, "answered": 0, "refused": 0})
        b["total"] += 1
        try:
            r = agent_ask(q["question"], cfg, max_iters=max_iters)
        except Exception as e:
            log.warning("Agent 失败 q%d: %s", q["question_id"], e)
            stats["failures"].append({"id": q["question_id"], "q": q["question"][:60],
                                      "error": str(e)})
            continue
        pred = r["answer"] or ""
        expected_tool = expected.get(q["question_id"], "search_report")
        tools_used = [t["tool"] for t in r["trace"]]
        route_ok = expected_tool in tools_used
        stats["route_hit"] += int(route_ok)
        b["route_hit"] += int(route_ok)

        ts = _tool_stats(r["trace"])
        stats["iters"] += r["iterations"]
        stats["tool_calls"] += ts["calls"]
        stats["tool_failed"] += ts["failed"]

        if _is_refusal(pred):
            stats["refused"] += 1
            b["refused"] += 1

        ok, kind = _judge_answer(q, pred, skip_judge)
        if kind == "numeric":
            stats["numeric_total"] += 1
            stats["numeric_correct"] += int(bool(ok))
        elif kind == "text":
            stats["text_total"] += 1
            stats["text_correct"] += int(bool(ok))
        if ok is not None:
            stats["answer_total"] += 1
            stats["answer_correct"] += int(bool(ok))
            b["answered"] += 1
            b["correct"] += int(bool(ok))
        if ok is False:
            stats["failures"].append({
                "id": q["question_id"], "q": q["question"][:60],
                "gold": q["answer"][:100], "pred": pred[:160],
                "tools": tools_used, "expected_tool": expected_tool})
        if i % 10 == 0:
            print(f"  {i}/{len(qs)} 完成，用时 {time.time() - t0:.0f}s")

    # ---- 多步任务 ----
    print(f"多步任务 {len(MULTI_STEP_TASKS)} 条…")
    multi = []
    for t in MULTI_STEP_TASKS:
        try:
            r = agent_ask(t["question"], cfg, max_iters=max_iters)
        except Exception as e:
            multi.append({"id": t["id"], "question": t["question"], "error": str(e)})
            continue
        tools_used = [x["tool"] for x in r["trace"]]
        hit = [x for x in t["expected_tools"] if x in tools_used]
        route_score = len(hit) / len(t["expected_tools"])
        judge = None if skip_judge else _llm_judge(
            t["question"], "应使用工具：" + "、".join(t["expected_tools"]),
            (r["answer"] or "")[:500])
        multi.append({"id": t["id"], "question": t["question"], "tools": tools_used,
                      "expected": t["expected_tools"], "route_score": round(route_score, 2),
                      "judge": judge, "iterations": r["iterations"],
                      "answer": (r["answer"] or "")[:300]})

    # ---- 报告 ----
    def pct(a, b):
        return round(a / b * 100, 1) if b else None

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    report = paths.dir_reports() / f"agent_eval_{ts}.md"
    summary = {
        "questions": len(qs),
        "route_acc": pct(stats["route_hit"], stats["total"]),
        "answer_acc": pct(stats["answer_correct"], stats["answer_total"]),
        "numeric_acc": pct(stats["numeric_correct"], stats["numeric_total"]),
        "numeric_n": stats["numeric_total"],
        "text_acc": pct(stats["text_correct"], stats["text_total"]) if not skip_judge else None,
        "text_n": stats["text_total"],
        "refused": stats["refused"],
        "avg_iters": round(stats["iters"] / stats["total"], 2) if stats["total"] else None,
        "tool_calls": stats["tool_calls"],
        "tool_fail_rate": pct(stats["tool_failed"], stats["tool_calls"]),
        "multi_route_avg": round(sum(m.get("route_score", 0) for m in multi) / len(multi), 2)
        if multi else None,
        "report": str(report),
    }
    name_by_code = {c.code: c.name for c in cfg.companies}
    lines = [f"# Agent 评估报告 {datetime.now().strftime('%Y-%m-%d %H:%M')}", "",
             f"- CFQA 抽样：{len(qs)} 题（每公司最多 {per_company} 题，seed 42）",
             f"- **路由正确率**：{summary['route_acc']}%（期望工具出现在 trace）",
             f"- **答案准确率**：{summary['answer_acc']}%（判分题 {stats['answer_total']}）",
             f"  - 数值题：{summary['numeric_acc']}%（{stats['numeric_total']} 题）",
             f"  - 文本题：{summary['text_acc']}%（{stats['text_total']} 题）" if not skip_judge
             else f"  - 文本题 {stats['text_total']} 题（跳过判分）",
             f"- 拒答/未披露：{stats['refused']} 题",
             f"- 平均迭代 {summary['avg_iters']} 轮，工具调用 {stats['tool_calls']} 次，"
             f"失败率 {summary['tool_fail_rate']}%",
             "", "## 按公司", "", "| 公司 | 题数 | 路由正确 | 答案正确 | 拒答 |",
             "|---|---|---|---|---|"]
    for code, b in sorted(per_company_stats.items()):
        lines.append(f"| {name_by_code.get(code, code)}（{code}） | {b['total']} | "
                     f"{pct(b['route_hit'], b['total'])}% | "
                     f"{b['correct']}/{b['answered']} | {b['refused']} |")
    lines += ["", "## 多步任务", "",
              "| ID | 任务 | 期望工具 | 实际工具 | 路由分 | 判分 |",
              "|---|---|---|---|---|---|"]
    for m in multi:
        lines.append(f"| {m['id']} | {m['question'][:34]} | "
                     f"{'、'.join(m.get('expected', []))} | "
                     f"{'、'.join(m.get('tools', []))} | {m.get('route_score')} | "
                     f"{m.get('judge')} |")
    lines += ["", "## 失败样例（答案判错）", ""]
    for f in stats["failures"][:20]:
        if "error" in f:
            lines.append(f"- [{f['id']}] 异常：{f['error']}")
        else:
            lines.append(f"- [{f['id']}] {f['q']}")
            lines.append(f"  - gold: {f['gold']}")
            lines.append(f"  - pred: {f['pred']}")
            lines.append(f"  - tools: {f.get('tools')}（期望 {f.get('expected_tool')}）")
    report.write_text("\n".join(lines), encoding="utf-8")
    log.info("Agent 评估完成: %s", summary)
    conn.close()
    return {**summary, "multi": multi}
