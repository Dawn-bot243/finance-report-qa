"""Agent 工具集：5 个财报问答工具（schema + 实现 + 分发）。

全部复用现有能力，不重写检索/对比/核验逻辑：
- search_report     → retriever.retrieve + generator.generate（带页码引用）
- get_metric        → compare._best_row/_display（结构化指标 + 出处）
- compare_companies → analysis.compare.compare_peers + render_markdown
- explain_metric    → kb.metric_kb.match_metric + format_entry
- verify_numbers    → analysis.verify.verify_numbers + format_findings

工具返回统一截断到 MAX_RESULT_CHARS，防止占满 Agent 上下文。
"""
from __future__ import annotations

import sqlite3

from ..analysis.compare import CORE_METRICS, _best_row, _display, compare_peers, render_markdown
from ..analysis.verify import format_findings, verify_numbers
from ..config import Config
from ..kb.metric_kb import format_entry, match_metric
from ..logutil import setup_logging
from ..metrics.defs import METRIC_BY_KEY
from ..rag.generator import generate
from ..rag.retriever import retrieve

log = setup_logging(__name__)

MAX_RESULT_CHARS = 4000
DEFAULT_METRICS = ["revenue", "net_profit_parent", "roe_weighted"]


def _clip(s: str, limit: int = MAX_RESULT_CHARS) -> str:
    return s if len(s) <= limit else s[:limit] + "\n…（结果已截断）"


def _company_catalog(cfg: Config) -> str:
    return "、".join(f"{c.name}({c.code})" for c in cfg.companies)


def _years(cfg: Config) -> str:
    ys = sorted(cfg.fiscal_years)
    return f"{ys[0]}-{ys[-1]}" if ys else "2019-2022"


def build_tools(cfg: Config) -> list[dict]:
    """生成 function-calling schema（公司清单与财年写进 description，帮助模型选参）。"""
    companies = _company_catalog(cfg)
    years = _years(cfg)
    code_desc = f"公司股票代码（6 位）。可选：{companies}"
    year_desc = f"报告期年份，{years} 之一"
    metric_desc = ("指标 key 数组，如 [\"revenue\", \"net_profit_parent\", \"roe_weighted\", "
                   "\"gross_margin\"]；留空则返回营业收入/归母净利润/加权ROE 三项")

    return [
        {"type": "function", "function": {
            "name": "get_metric",
            "description": "查询单个公司单个财年的结构化财务指标（含年报页码出处）。"
                           "适用于「某公司某年某指标是多少」这类点查问题。",
            "parameters": {"type": "object", "properties": {
                "code": {"type": "string", "description": code_desc},
                "year": {"type": "integer", "description": year_desc},
                "metric_keys": {"type": "array", "items": {"type": "string"},
                                "description": metric_desc},
            }, "required": ["code", "year"]},
        }},
        {"type": "function", "function": {
            "name": "search_report",
            "description": "在年报/招股书全文中做语义检索并生成带页码引用的回答。"
                           "适用于开放性问题、叙述性内容（研发进展、业务模式、风险因素等），"
                           "或结构化指标查不到时兜底。",
            "parameters": {"type": "object", "properties": {
                "question": {"type": "string",
                             "description": "要检索的问题（保留公司名与年份，可自然语言）"},
            }, "required": ["question"]},
        }},
        {"type": "function", "function": {
            "name": "compare_companies",
            "description": "把 2-3 家公司在同一报告期的财务指标生成对比表（含可比性提示）。"
                           "适用于「A 和 B 谁/对比/差异」类问题。",
            "parameters": {"type": "object", "properties": {
                "codes": {"type": "array", "items": {"type": "string"},
                          "description": f"2-3 个股票代码。可选：{companies}"},
                "year": {"type": "integer", "description": year_desc},
                "metric_keys": {"type": "array", "items": {"type": "string"},
                                "description": "对比指标 key 数组；留空取核心 15 项"},
            }, "required": ["codes", "year"]},
        }},
        {"type": "function", "function": {
            "name": "explain_metric",
            "description": "解释财务指标的含义、公式、解读方法与注意事项（知识库，22 个核心指标）。"
                           "适用于「ROE 是什么意思」「毛利率怎么算」这类概念问题。",
            "parameters": {"type": "object", "properties": {
                "metric": {"type": "string",
                           "description": "指标名称或 key，如 ROE、毛利率、revenue、资产负债率"},
            }, "required": ["metric"]},
        }},
        {"type": "function", "function": {
            "name": "verify_numbers",
            "description": "核验一段文字中的「指标=数值」陈述是否与结构化指标库一致（容差 0.5%），"
                           "返回一致/待核实清单。适用于用户给出数字让你核对。",
            "parameters": {"type": "object", "properties": {
                "text": {"type": "string", "description": "待核验的文本"},
                "code": {"type": "string", "description": "限定公司代码（可选）"},
                "year": {"type": "integer", "description": "限定财年（可选）"},
            }, "required": ["text"]},
        }},
    ]


# ---------------------------------------------------------------- 工具实现
def _tool_get_metric(ctx: dict, args: dict) -> str:
    conn: sqlite3.Connection = ctx["conn"]
    code = str(args.get("code", "")).strip()
    year = args.get("year")
    keys = args.get("metric_keys") or DEFAULT_METRICS
    if not code or not year:
        return "参数缺失：需要 code 与 year。"
    row = conn.execute("SELECT name FROM companies WHERE code=?", (code,)).fetchone()
    name = row["name"] if row else code
    lines = [f"{name}（{code}）{year} 年财务指标："]
    for key in keys[:5]:
        if key not in METRIC_BY_KEY:
            lines.append(f"- {key}：未知指标 key")
            continue
        r = _best_row(conn, code, int(year), key)
        cn = METRIC_BY_KEY[key].cn_name
        if r is None or r["value"] is None:
            lines.append(f"- {cn}：未披露")
            continue
        page = r["page_no"] or r["evidence_page_no"]
        cite = f"，出处【{r['title']} 第{page}页】" if page else "（无页码出处）"
        lines.append(f"- {cn}：{_display(float(r['value']), key)}"
                     f"（口径 {r['basis']}，来源 {r['source']}{cite}）")
    return "\n".join(lines)


def _tool_search_report(ctx: dict, args: dict) -> str:
    question = str(args.get("question", "")).strip()
    if not question:
        return "参数缺失：需要 question。"
    chunks = retrieve(question, ctx["cfg"], top_k=5)  # 与 RAG 直答保持一致
    gen = generate(question, chunks)
    return gen["answer"]


def _tool_compare(ctx: dict, args: dict) -> str:
    conn: sqlite3.Connection = ctx["conn"]
    codes = [str(c).strip() for c in (args.get("codes") or [])]
    year = args.get("year")
    if not (2 <= len(codes) <= 3) or not year:
        return "参数非法：需要 2-3 个 codes 与 year。"
    keys = args.get("metric_keys") or None
    res = compare_peers(conn, codes, int(year), keys)
    return render_markdown(res)


def _tool_explain(ctx: dict, args: dict) -> str:
    metric = str(args.get("metric", "")).strip()
    if not metric:
        return "参数缺失：需要 metric。"
    entry = match_metric(metric)
    if entry is None:
        return f"知识库未收录「{metric}」。可尝试营收、净利润、ROE、毛利率、资产负债率等核心指标。"
    return format_entry(entry)


def _tool_verify(ctx: dict, args: dict) -> str:
    conn: sqlite3.Connection = ctx["conn"]
    text = str(args.get("text", "")).strip()
    if not text:
        return "参数缺失：需要 text。"
    code = args.get("code") or None
    year = args.get("year")
    res = verify_numbers(conn, text, code, int(year) if year else None)
    head = f"共核验 {res['total']} 处指标陈述：一致 {res['matched']} 处，待核实 {len(res['suspicious'])} 处。"
    note = format_findings(res)
    return head + ("\n" + note if note else "\n未发现数值不一致。")


_DISPATCH = {
    "get_metric": _tool_get_metric,
    "search_report": _tool_search_report,
    "compare_companies": _tool_compare,
    "explain_metric": _tool_explain,
    "verify_numbers": _tool_verify,
}


def dispatch(name: str, args: dict, ctx: dict) -> str:
    """执行工具并截断输出。未知工具名返回可读错误给模型自我修正。"""
    fn = _DISPATCH.get(name)
    if fn is None:
        return f"未知工具「{name}」。可用工具：{', '.join(_DISPATCH)}。"
    try:
        return _clip(fn(ctx, args))
    except Exception as e:
        log.exception("工具 %s 执行失败", name)
        return f"工具「{name}」执行失败：{e}。请调整参数或改用其他工具。"
