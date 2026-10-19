"""多智能体辩论：看多 vs 看空 → N 轮交锋 → 主笔汇总尽调报告。

每个辩手都是完整的工具调用 Agent（各自实时检索取证），主笔也挂工具用于核实引用。
输出报告中的所有数字与引用均来自工具结果（【文档名 第N页】）。
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime

from .. import paths
from ..config import Config, load
from ..logutil import setup_logging
from .loop import run_tool_loop
from .tools import build_tools

log = setup_logging(__name__)

MAX_SPEECH_CHARS = 1200   # 传给对方的发言截断长度
MAX_HISTORY_ROUNDS = 2    # 传给辩手的历史轮数（控制上下文）

BULL_SYSTEM = """你是 A 股上市公司研究中的**看多研究员**。你的任务是在多空辩论中构建看多论证。

规则：
1. 只能通过工具获取证据（get_metric / search_report / compare_companies 等），不得使用外部知识。
2. 每个关键论点必须有工具证据支撑，引用格式【文档名 第N页】。
3. 优先挖掘：营收/利润增长、盈利能力改善、现金流健康、行业地位、研发投入、市场份额、分红回报。
4. 对看空方的质疑，要么用证据反驳，要么承认其合理性并说明为何不改变整体判断。
5. 不得编造数字；工具明确说「未披露」时如实说明。
6. 每次发言控制在 400 字以内，结构清晰（论点 + 证据 + 回应）。"""

BEAR_SYSTEM = """你是 A 股上市公司研究中的**看空研究员**。你的任务是在多空辩论中构建看空论证。

规则：
1. 只能通过工具获取证据（get_metric / search_report / compare_companies 等），不得使用外部知识。
2. 每个关键论点必须有工具证据支撑，引用格式【文档名 第N页】。
3. 优先挖掘：营收/利润下滑、亏损与减值、现金流恶化、负债与偿债压力、商誉风险、客户/供应商集中、行业竞争、治理与关联交易风险。
4. 对看多方的质疑，要么用证据反驳，要么承认其合理性并说明为何不改变整体判断。
5. 不得编造数字；工具明确说「未披露」时如实说明。
6. 每次发言控制在 400 字以内，结构清晰（论点 + 证据 + 回应）。"""

MODERATOR_SYSTEM = """你是尽调报告主笔。基于多空双方的辩论记录，输出一份客观、可溯源的尽调报告。

输出结构（Markdown）：
# {company}（{code}）{year} 年度尽调报告
## 一、核心结论
用 2-3 句话给出判断（偏多 / 偏空 / 中性），并说明最主要的一条理由。
## 二、共识
双方都认可的事实（列 3-5 条，每条带引用）。
## 三、分歧
逐条列出双方观点冲突之处（3-5 条），每条写「看多方认为…看空方认为…」，并给出证据强弱判断。
## 四、关键证据
按主题整理支撑报告的核心数据（指标 + 数值 + 出处）。
## 五、风险提示
需要持续跟踪的风险点与信息缺口。

硬性要求：
1. 所有数字必须来自辩论记录中的工具证据，不得编造或推算。
2. 引用保持【文档名 第N页】原格式；记录中没有出处的论断标注「（无出处）」。
3. 可以用 verify_numbers 工具核验关键数字。
4. 若双方证据不足以支撑明确结论，如实写「证据不足，倾向中性」。"""


def _debate_messages(system: str, topic: str, history: list[dict],
                     instruction: str) -> list[dict]:
    """构造辩手本轮的消息：system + 主题 + 最近若干轮发言 + 本轮指令。

    每轮重建（不保留历史 tool 结果），控制上下文长度。
    """
    msgs = [{"role": "system", "content": system},
            {"role": "user", "content": f"辩论主题：{topic}"}]
    for h in history[-MAX_HISTORY_ROUNDS * 2:]:
        side = "看多方" if h["side"] == "bull" else "看空方"
        msgs.append({"role": "user",
                     "content": f"【{side}第{h['round']}轮发言】\n{h['content'][:MAX_SPEECH_CHARS]}"})
    msgs.append({"role": "user", "content": instruction})
    return msgs


def _transcript(rounds: list[dict]) -> str:
    parts = []
    for h in rounds:
        side = "看多" if h["side"] == "bull" else "看空"
        parts.append(f"### 第{h['round']}轮 · {side}方\n{h['content']}")
    return "\n\n".join(parts)


def debate(code: str, year: int, cfg: Config | None = None, rounds: int = 3,
           max_iters: int = 4, verbose: bool = False) -> dict:
    """运行多空辩论并生成尽调报告。

    返回 {report, path, history, rounds, elapsed_s}
    """
    cfg = cfg or load()
    if not 1 <= rounds <= 5:
        raise ValueError("rounds 需在 1-5 之间")
    conn = sqlite3.connect(paths.db_path())
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT name FROM companies WHERE code=?", (code,)).fetchone()
    conn.close()
    if not row:
        raise ValueError(f"未知公司代码 {code}")
    name = row["name"]
    topic = f"{name}（{code}）{year} 年度财报的多空分析"
    tools = build_tools(cfg)
    t0 = time.time()

    history: list[dict] = []
    for r in range(1, rounds + 1):
        # ---- 看多发言 ----
        bull_msgs = _debate_messages(
            BULL_SYSTEM, topic, history,
            f"请进行第 {r} 轮看多论证：先用工具取证，再给出观点。")
        try:
            bull = run_tool_loop(bull_msgs, cfg, tools, max_iters=max_iters,
                                 verbose=verbose, label=f"bull r{r}")
        except Exception as e:
            log.warning("看多方第%d轮发言失败: %s", r, e)
            bull = {"answer": f"（本轮发言因接口异常未完成：{e}）", "trace": []}
        history.append({"round": r, "side": "bull", "content": bull["answer"],
                        "trace": bull["trace"]})
        if verbose:
            print(f"[辩论] 第{r}轮 看多发言 {len(bull['answer'])}字")

        # ---- 看空发言（看到看多本轮观点）----
        bear_msgs = _debate_messages(
            BEAR_SYSTEM, topic, history,
            f"看多方刚给出第 {r} 轮观点（见上）。请针对其论点进行看空反驳，"
            f"先用工具补充证据，再给出观点。")
        try:
            bear = run_tool_loop(bear_msgs, cfg, tools, max_iters=max_iters,
                                 verbose=verbose, label=f"bear r{r}")
        except Exception as e:
            log.warning("看空方第%d轮发言失败: %s", r, e)
            bear = {"answer": f"（本轮发言因接口异常未完成：{e}）", "trace": []}
        history.append({"round": r, "side": "bear", "content": bear["answer"],
                        "trace": bear["trace"]})
        if verbose:
            print(f"[辩论] 第{r}轮 看空发言 {len(bear['answer'])}字")

    # ---- 主笔汇总 ----
    mod_msgs = [{"role": "system", "content": MODERATOR_SYSTEM.format(
                    company=name, code=code, year=year)},
                {"role": "user", "content": f"【辩论记录】\n\n{_transcript(history)}\n\n"
                                            f"请输出尽调报告。"}]
    mod = run_tool_loop(mod_msgs, cfg, tools, max_iters=3, verbose=verbose,
                        label="moderator", max_tokens=3000)

    elapsed = round(time.time() - t0, 1)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = paths.dir_reports() / f"debate_{code}_{year}_{ts}.md"
    header = (f"<!-- 自动生成：多空辩论尽调报告 | {name}({code}) FY{year} | "
              f"{rounds} 轮 | 耗时 {elapsed}s -->\n\n")
    out.write_text(header + mod["answer"], encoding="utf-8")
    log.info("尽调报告已生成: %s（%d 轮，%.0fs）", out, rounds, elapsed)
    return {"report": mod["answer"], "path": str(out), "history": history,
            "rounds": rounds, "elapsed_s": elapsed,
            "tool_calls": sum(len(h["trace"]) for h in history) + len(mod["trace"])}


def print_debate(result: dict) -> None:
    print(f"\n{'=' * 60}\n尽调报告\n{'=' * 60}\n")
    print(result["report"])
    print(f"\n{'=' * 60}")
    print(f"辩论 {result['rounds']} 轮，工具调用 {result['tool_calls']} 次，"
          f"耗时 {result['elapsed_s']}s")
    print(f"报告：{result['path']}")
    print("\n辩论轨迹：")
    for h in result["history"]:
        side = "看多" if h["side"] == "bull" else "看空"
        tools_used = "、".join(t["tool"] for t in h["trace"]) or "无工具"
        print(f"  第{h['round']}轮 {side}：{len(h['content'])}字 | 工具：{tools_used}")
