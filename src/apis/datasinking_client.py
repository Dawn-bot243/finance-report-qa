"""datasinking 客户端：拉取年报全文 Markdown → report_markdown 表；
解析「主要会计数据/主要财务指标」Markdown 表格 → metrics（source='datasinking'）。"""
from __future__ import annotations

import re
import sqlite3

from datasinking import DataSinking

from ..config import Company, datasink_key, fmp_symbol
from ..logutil import setup_logging
from ..metrics.anchor import anchor_rows, parse_number
from ..metrics.defs import METRIC_DEFS, S2_DATA, S2_RATIO
from ..metrics.tabparse import clean_table, detect_columns
from ..metrics.units import pick_unit
from .baostock_client import _upsert_api_metric

log = setup_logging(__name__)

_PIPE_ROW = re.compile(r"^\s*\|")


def _markdown_tables(content: str) -> list[list[list[str]]]:
    """提取 Markdown 表格：连续 | 行成组，去分隔行 |---|---|。"""
    tables: list[list[list[str]]] = []
    cur: list[list[str]] = []
    for line in content.splitlines():
        if _PIPE_ROW.match(line):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
                continue  # 分隔行
            cur.append(cells)
        else:
            if cur:
                tables.append(clean_table(cur))
                cur = []
    if cur:
        tables.append(clean_table(cur))
    return tables


def _table_kind(rows: list[list[str]]) -> str | None:
    text = "".join(str(c or "") for r in rows for c in r)
    if "主要会计数据" in text[:200] or ("营业总收入" in text and "归属于上市公司股东的净利润" in text and len(rows) <= 12):
        return S2_DATA
    if "主要财务指标" in text[:200] or ("加权平均净资产收益率" in text and "基本每股收益" in text and len(rows) <= 12):
        return S2_RATIO
    return None


def _extract_markdown_metrics(conn: sqlite3.Connection, code: str, fy: int,
                              rows: list[list[str]], kind: str, api_ref: str) -> int:
    """从 Markdown 表格行提取指标（与 PDF 锚定同逻辑，出处为 datasinking）。"""
    col_map = detect_columns(rows, kind, fy)
    if "本期" not in col_map:
        return 0
    factor, _unit, _src = pick_unit(rows, [])
    n = 0
    for mdef in METRIC_DEFS:
        if kind not in mdef.tables or mdef.yoy_of:
            continue
        for row_i in anchor_rows(rows, mdef, kind):
            for fy_col, col_label in [(fy, "本期"), (fy - 1, "上年同期")]:
                ci = col_map.get(col_label)
                if ci is None or ci >= len(rows[row_i]):
                    continue
                cell = str(rows[row_i][ci] or "").strip()
                val = parse_number(cell)
                if val is None:
                    continue
                if mdef.unit_type == "currency":
                    val = val * factor
                _upsert_api_metric(
                    conn, code, fy_col, mdef.key, val, cell,
                    source="datasinking", api_ref=api_ref,
                    source_text="datasinking markdown: " + " | ".join(rows[row_i][:6])[:200],
                    basis="as_reported",
                )
                n += 1
    return n


def fetch_datasinking(conn: sqlite3.Connection, companies: list[Company], years: list[int]) -> dict:
    """拉取年报 Markdown 入库 + 解析指标。返回统计。"""
    ds = DataSinking(datasink_key())
    stats = {"markdown_docs": 0, "metric_rows": 0, "missing": []}
    for company in companies:
        symbol = fmp_symbol(company.code)
        try:
            # 服务端过滤：年报 + 报告期区间；limit=-1 自动分页拉全
            reports = ds.get_stock_reports(
                symbol, doc_type="annual",
                period_from=f"{min(years)}-01-01", period_to=f"{max(years)}-12-31",
                limit=-1,
            )
        except Exception as e:
            log.warning("%s datasinking 拉取失败: %s", symbol, e)
            stats["missing"].append(f"{symbol} fetch failed")
            continue
        for r in reports:
            fy = int(r["report_period"][:4])
            if fy not in years:
                continue
            content = r.get("content") or ""
            if not content:
                stats["missing"].append(f"{symbol} {fy} 无内容")
                continue
            conn.execute(
                """INSERT INTO report_markdown (code, symbol, report_period, doc_type, title,
                                                adjunct_url, content, source_api)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT (code, report_period, doc_type)
                   DO UPDATE SET content=excluded.content, title=excluded.title,
                                 adjunct_url=excluded.adjunct_url, fetched_at=datetime('now','localtime')""",
                (company.code, symbol, r["report_period"], "annual_report", r.get("title"),
                 r.get("adjunct_url"), content, "datasinking"),
            )
            stats["markdown_docs"] += 1
            # 解析第二节指标表
            for table_rows in _markdown_tables(content):
                kind = _table_kind(table_rows)
                if kind is None:
                    continue
                n = _extract_markdown_metrics(
                    conn, company.code, fy, table_rows, kind,
                    api_ref=f"datasinking:{symbol}:{r['report_period']}",
                )
                stats["metric_rows"] += n
    conn.commit()
    log.info("datasinking 完成: %s", stats)
    return stats
