"""Agent 循环：LLM 自主选择工具 → 执行 → 观察结果 → 继续或作答。

单轮多步（一次提问内的多工具循环），不保留跨问题会话记忆。
"""
from __future__ import annotations

import json
import sqlite3
import time

from .. import paths
from ..config import Config, load
from ..logutil import setup_logging
from ..rag.clients import chat_tools
from .tools import build_tools, dispatch

log = setup_logging(__name__)

MAX_TOOL_CONTEXT = 20000  # 累计工具结果上限，超过则提示模型收尾

SYSTEM_PROMPT = """你是 A 股上市公司年报问答助手。你只能通过调用工具获取证据，不得使用外部知识。

工具路由规则：
- 问某公司某年的具体财务指标 → get_metric
- 问两家以上公司的对比 → compare_companies
- 问指标含义/公式/怎么算 → explain_metric
- 需要核验一段文字里的数字 → verify_numbers
- 开放性问题、叙述性内容（研发、业务、风险、客户等）→ search_report

铁律：
1. 回答中的每个数字与事实必须来自工具返回结果，不得编造或推算。
2. 引用必须标注【文档名 第N页】（工具结果里已带出处，照搬即可）。
3. 工具明确返回「未披露」时，回答「年报中未披露该信息」，不要猜。
4. 不要用相同参数重复调用同一个工具；已有证据足够时立即作答。
5. 用简体中文简洁回答，先给结论再列证据。

候选公司与财年（工具参数用 6 位代码与年份）：
{companies}
财年：{years}
"""


def _system_prompt(cfg: Config) -> str:
    companies = "、".join(f"{c.name}({c.code})" for c in cfg.companies)
    ys = sorted(cfg.fiscal_years)
    years = f"{ys[0]}-{ys[-1]}" if ys else "2019-2022"
    return SYSTEM_PROMPT.format(companies=companies, years=years)


def run_tool_loop(messages: list[dict], cfg: Config, tools: list[dict] | None = None,
                  max_iters: int = 6, verbose: bool = False,
                  corrective: bool = True, label: str = "agent",
                  max_tokens: int = 1500) -> dict:
    """在给定消息历史上跑工具调用循环（会就地追加 messages）。

    供 agent_ask（单 Agent 问答）与 debate（多智能体辩论）复用。
    返回 {answer, trace, iterations, forced}。
    """
    conn = sqlite3.connect(paths.db_path())
    conn.row_factory = sqlite3.Row
    ctx = {"conn": conn, "cfg": cfg, "corrective": corrective}
    tools = tools if tools is not None else build_tools(cfg)
    trace: list[dict] = []
    seen: set[str] = set()
    tool_chars = 0

    try:
        for it in range(1, max_iters + 1):
            msg = chat_tools(messages, tools, max_tokens=max_tokens)
            tool_calls = msg.get("tool_calls") or []
            content = msg.get("content") or ""

            if not tool_calls:
                if verbose:
                    print(f"[{label}] {it} 轮后作答，共调用工具 {len(trace)} 次")
                return {"answer": content, "trace": trace, "iterations": it, "forced": False}

            # 回填 assistant 消息（tool_calls 必须原样带回）
            messages.append({"role": "assistant", "content": content, "tool_calls": tool_calls})

            for tc in tool_calls:
                fn = (tc.get("function") or {}).get("name", "")
                raw = (tc.get("function") or {}).get("arguments") or "{}"
                t0 = time.time()
                ok = True
                if fn not in {t["function"]["name"] for t in tools}:
                    ok = False
                    result = (f"未知工具「{fn}」。可用工具：get_metric, search_report, "
                              f"compare_companies, explain_metric, verify_numbers。")
                else:
                    try:
                        args = json.loads(raw)
                        if not isinstance(args, dict):
                            raise ValueError("arguments 必须是 JSON 对象")
                    except Exception as e:
                        ok = False
                        result = f"参数非法：{e}。请用合法 JSON 重新调用该工具。"
                        args = {}
                    else:
                        key = (fn, json.dumps(args, sort_keys=True, ensure_ascii=False))
                        if key in seen:
                            result = "该工具与参数已调用过，结果见上一条。请勿重复调用，基于已有证据作答。"
                        else:
                            seen.add(key)
                            result = dispatch(fn, args, ctx)

                messages.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                                 "content": result})
                tool_chars += len(result)
                elapsed = round((time.time() - t0) * 1000)
                trace.append({"tool": fn, "args": raw[:200], "ok": ok,
                              "chars": len(result), "elapsed_ms": elapsed,
                              "result_preview": result[:160].replace("\n", " ")})
                if verbose:
                    print(f"  [{label} {it}] {fn}({raw[:80]}) → {len(result)}字 {elapsed}ms | "
                          f"{result[:80]!r}")

            if tool_chars > MAX_TOOL_CONTEXT:
                messages.append({"role": "user",
                                 "content": "已收集足够证据，请立即基于上述工具结果作答，不要再调用工具。"})

        # 超过最大轮数：不带 tools 强制总结
        log.warning("[%s] 达到最大轮数 %d，强制总结", label, max_iters)
        messages.append({"role": "user",
                         "content": "已达最大工具调用轮数。请基于已有工具结果直接作答；"
                                    "证据不足时明确说明缺少什么。"})
        final = chat_tools(messages, tools=None, max_tokens=max_tokens)
        return {"answer": final.get("content") or "", "trace": trace,
                "iterations": max_iters, "forced": True}
    finally:
        conn.close()


def agent_ask(question: str, cfg: Config | None = None, max_iters: int = 6,
              verbose: bool = False, corrective: bool = True) -> dict:
    """运行一次 Agent 问答。返回 {answer, trace, iterations, forced}。

    corrective=False 时 search_report 退回单次检索（用于对照评估）。
    """
    cfg = cfg or load()
    messages: list[dict] = [
        {"role": "system", "content": _system_prompt(cfg)},
        {"role": "user", "content": question},
    ]
    return run_tool_loop(messages, cfg, build_tools(cfg), max_iters=max_iters,
                         verbose=verbose, corrective=corrective, label="agent")


def print_agent_answer(result: dict) -> None:
    print(f"\n回答：\n{result['answer']}")
    print(f"\n迭代 {result['iterations']} 轮，工具调用 {len(result['trace'])} 次"
          + ("（强制收尾）" if result.get("forced") else ""))
    if result["trace"]:
        print("工具轨迹：")
        for i, t in enumerate(result["trace"], 1):
            status = "✓" if t["ok"] else "✗"
            print(f"  {i}. {status} {t['tool']} {t['args'][:100]} → "
                  f"{t['chars']}字/{t['elapsed_ms']}ms")
