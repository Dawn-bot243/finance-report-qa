"""结构化直答：查询分析命中「公司+年份+指标」时，直接查 metrics 表回答（带出处）。

优先级：as_reported（与 CFQA 标准答案同口径）> restated 佐证。
出处：PDF 行的 doc_id/page_no，API 行的 evidence_doc_id/evidence_page_no。
精度守卫：分部问题（某产品/业务/行业的收入）与同比问题不按总量直答。
"""
from __future__ import annotations

import re
import sqlite3

from .. import paths
from ..logutil import setup_logging
from ..metrics.defs import METRIC_BY_KEY
from .retriever import QueryInfo

log = setup_logging(__name__)

_SEGMENT_PAT = re.compile(r"产品|业务|行业|地区|境内|境外|分部|分类|渠道|经销|直销|自产")
_YOY_PAT = re.compile(r"比上年|同比|增减|增长率|增速|变动比例|变化")

# 指标 key → 中文问题表述（用于生成回答句）
_ASK_PHRASE = {
    "revenue": "营业收入", "net_profit": "净利润",
    "net_profit_parent": "归属于母公司股东的净利润", "net_profit_deduct": "扣除非经常性损益后归母净利润",
    "roe_weighted": "加权平均净资产收益率", "eps_basic": "基本每股收益", "eps_diluted": "稀释每股收益",
    "bps": "每股净资产", "total_assets": "资产总额", "net_assets_parent": "归属于母公司股东的净资产",
    "total_liabilities": "负债合计", "asset_liability_ratio": "资产负债率",
    "ocf": "经营活动产生的现金流量净额", "icf": "投资活动产生的现金流量净额",
    "fcf": "筹资活动产生的现金流量净额", "gross_margin": "毛利率", "net_margin": "净利率",
}


def structured_answer(conn: sqlite3.Connection, info: QueryInfo,
                      question: str = "") -> str | None:
    """命中则返回带引用的回答文本；未命中（含精度守卫拦截）返回 None。"""
    if not info.is_structured:
        return None
    # 精度守卫：分部/产品维度问题不能按总量直答
    if _SEGMENT_PAT.search(question):
        return None
    # 同比问题 → 查 _yoy 指标；没有对应 _yoy 定义的指标不能直答（退回 RAG）
    yoy = bool(_YOY_PAT.search(question))
    answers = []
    for key in info.metric_keys[:3]:  # 一个问题最多答 3 个指标
        if yoy:
            key2 = key + "_yoy"
            if key2 in METRIC_BY_KEY:
                key = key2
            else:
                continue
        mdef = METRIC_BY_KEY[key]
        # 1) as_reported：PDF 行优先（confidence 高者先），其次 datasinking
        row = conn.execute(
            """SELECT m.*, d.title FROM metrics m JOIN documents d ON m.doc_id=d.doc_id
               WHERE m.code=? AND m.fiscal_year=? AND m.metric_key=?
                 AND m.basis='as_reported' AND m.doc_id IS NOT NULL
                 AND (m.column_label='本期' OR m.column_label=CAST(m.fiscal_year AS TEXT))
               ORDER BY (m.source='prospectus') ASC, m.confidence DESC, m.metric_id LIMIT 1""",
            (info.code, info.year, key),
        ).fetchone()
        if row is None:
            row = conn.execute(
                """SELECT m.*, d.title FROM metrics m
                   LEFT JOIN documents d ON m.evidence_doc_id=d.doc_id
                   WHERE m.code=? AND m.fiscal_year=? AND m.metric_key=?
                     AND m.basis='as_reported' AND m.doc_id IS NULL
                   ORDER BY m.metric_id LIMIT 1""",
                (info.code, info.year, key),
            ).fetchone()
        if row is None:
            continue
        value = row["value"]
        if mdef.unit_type == "currency":
            disp = f"{value:,.2f} 元"
        elif mdef.unit_type in ("percent",):
            disp = f"{value:.2f}%"
        else:
            disp = f"{value:,.4f}"
        # 引用
        title = row["title"] or "?"
        if row["doc_id"] is not None and row["page_no"]:
            cite = f"【{title} 第{row['page_no']}页】"
        elif row["evidence_doc_id"] and row["evidence_page_no"]:
            cite = f"【{title} 第{row['evidence_page_no']}页】"
        elif row["evidence_doc_id"]:
            cite = f"【{title}】"
        else:
            cite = f"【{row['source']}】"
        answers.append(f"{info.company_name} {info.year} 年{_ASK_PHRASE.get(key, mdef.cn_name)}为 {disp} {cite}")
    if not answers:
        return None
    return "\n".join(answers)
