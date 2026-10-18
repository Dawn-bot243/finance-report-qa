"""同业对比：手动选 2-3 家公司 + 同一报告期，从结构化指标库生成对比表。

数据优先级：as_reported + PDF（原文披露）> as_reported + datasinking > restated（API 重述）。
可比性检查：缺失、口径混合、招股书数据（上市前）逐项标注。
"""
from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from .. import paths
from ..metrics.defs import METRIC_BY_KEY

# 默认对比指标（核心 15 项）
CORE_METRICS = [
    "revenue", "net_profit_parent", "net_profit_deduct", "net_profit",
    "roe_weighted", "gross_margin", "net_margin", "eps_basic", "bps",
    "total_assets", "net_assets_parent", "total_liabilities",
    "asset_liability_ratio", "ocf", "rd_expense",
]


@dataclass
class Cell:
    code: str
    value: float | None
    display: str
    basis: str | None
    source: str | None
    doc_title: str | None
    doc_type: str | None
    page_no: int | None
    status: str  # ok | missing


def _display(value: float | None, metric_key: str) -> str:
    if value is None:
        return "未披露"
    unit = METRIC_BY_KEY[metric_key].unit_type
    if unit == "currency":
        if abs(value) >= 1e8:
            return f"{value / 1e8:,.2f} 亿元"
        return f"{value / 1e4:,.2f} 万元"
    if unit == "percent":
        return f"{value:.2f}%"
    if unit == "pp":
        return f"{value:+.2f} 个百分点"
    if unit == "per_share":
        return f"{value:.4f} 元/股"
    if unit == "share":
        return f"{value / 1e8:,.2f} 亿股"
    return f"{value:,.2f}"


def _best_row(conn: sqlite3.Connection, code: str, year: int, key: str):
    return conn.execute(
        """SELECT m.*, d.title, d.doc_type
           FROM metrics m
           LEFT JOIN documents d ON COALESCE(m.doc_id, m.evidence_doc_id) = d.doc_id
           WHERE m.code=? AND m.fiscal_year=? AND m.metric_key=?
             AND (m.column_label IN ('本期','') OR m.column_label=CAST(m.fiscal_year AS TEXT))
           ORDER BY CASE m.basis WHEN 'as_reported' THEN 0 ELSE 1 END,
                    CASE WHEN m.method='table' THEN 0 ELSE 1 END,
                    CASE WHEN m.doc_id IS NOT NULL THEN 0 ELSE 1 END,
                    CASE WHEN m.source='prospectus' THEN 1 ELSE 0 END,
                    CASE m.confidence WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
                    m.metric_id
           LIMIT 1""",
        (code, year, key),
    ).fetchone()


def _cell(conn: sqlite3.Connection, code: str, year: int, key: str) -> Cell:
    row = _best_row(conn, code, year, key)
    if row is None or row["value"] is None:
        return Cell(code, None, "未披露", None, None, None, None, None, "missing")
    return Cell(code, float(row["value"]), _display(float(row["value"]), key),
                row["basis"], row["source"], row["title"], row["doc_type"],
                row["page_no"], "ok")


def _cell_source_label(c: Cell) -> str:
    parts = []
    if c.doc_title and c.page_no:
        short = "招股书" if c.doc_type == "prospectus" else "年报"
        parts.append(f"{short}p{c.page_no}")
    if c.basis == "restated":
        parts.append("API重述")
    elif c.basis == "as_reported" and c.source == "datasinking":
        parts.append("datasinking")
    return f"[{'·'.join(parts)}]" if parts else ""


def compare_peers(conn: sqlite3.Connection, codes: list[str], year: int,
                  metric_keys: list[str] | None = None) -> dict:
    keys = metric_keys or CORE_METRICS
    keys = [k for k in keys if k in METRIC_BY_KEY]
    companies = []
    for code in codes:
        r = conn.execute("SELECT name FROM companies WHERE code=?", (code,)).fetchone()
        companies.append((code, r["name"] if r else code))

    rows = []
    caveats: list[str] = []
    comparable = 0
    for key in keys:
        cells = [_cell(conn, code, year, key) for code, _ in companies]
        basis_set = {c.basis for c in cells if c.basis}
        type_set = {c.doc_type for c in cells if c.doc_type}
        missing = [c.code for c in cells if c.status == "missing"]
        row_caveats = []
        if missing:
            row_caveats.append(f"{len(missing)} 家未披露")
        if len(basis_set) > 1:
            row_caveats.append("口径混合（原文/重述）")
        if "prospectus" in type_set and "annual_report" in type_set:
            row_caveats.append("含招股书数据（上市前期）")
        if basis_set == {"restated"}:
            row_caveats.append("均为API重述值")
        if not row_caveats:
            comparable += 1
        rows.append({"key": key, "name": METRIC_BY_KEY[key].cn_name,
                     "cells": cells, "caveats": row_caveats})
        for rc in row_caveats:
            caveats.append(f"{METRIC_BY_KEY[key].cn_name}：{rc}")

    return {"year": year, "companies": companies, "rows": rows,
            "caveats": caveats, "comparable_rows": comparable,
            "total_rows": len(rows)}


def render_markdown(res: dict) -> str:
    names = [f"{name}（{code}）" for code, name in res["companies"]]
    lines = [f"# 同业对比 · {res['year']} 财年", "",
             f"**对比公司**：{'、'.join(names)}", "",
             "数据来自结构化指标库（as_reported 原文披露优先，API 重述兜底）。"
             "单元格附出处页码，`未披露` 表示该指标在年报中无数据。", "",
             "| 指标 | " + " | ".join(names) + " |",
             "|---|" + "---|" * len(names)]
    for row in res["rows"]:
        cells = []
        for c in row["cells"]:
            src = _cell_source_label(c)
            cells.append(f"{c.display}{src}")
        lines.append(f"| {row['name']} | " + " | ".join(cells) + " |")
    lines.append("")
    lines.append(f"**完全可比指标：{res['comparable_rows']}/{res['total_rows']}**")
    if res["caveats"]:
        lines.append("")
        lines.append("## 可比性说明")
        for c in res["caveats"]:
            lines.append(f"- {c}")
    lines.append("")
    lines.append("> 说明：同报告期、同口径的指标才严格可比；标注「未披露」的项不应视为 0，"
                 "「API重述」为追溯调整后的口径，可能与原文披露值存在差异。")
    return "\n".join(lines)


def write_reports(res: dict) -> tuple[str, str]:
    paths.ensure_dirs()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    md_path = paths.dir_reports() / f"peer_compare_{ts}.md"
    md_path.write_text(render_markdown(res), encoding="utf-8")
    csv_path = paths.dir_reports() / f"peer_compare_{ts}.csv"
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["metric_key", "metric_name", "code", "company", "value", "unit_type",
                    "basis", "source", "doc_title", "doc_type", "page_no", "status"])
        for row in res["rows"]:
            for (code, name), c in zip(res["companies"], row["cells"]):
                w.writerow([row["key"], row["name"], code, name,
                            c.value if c.value is not None else "",
                            METRIC_BY_KEY[row["key"]].unit_type,
                            c.basis or "", c.source or "", c.doc_title or "",
                            c.doc_type or "", c.page_no or "", c.status])
    return str(md_path), str(csv_path)
