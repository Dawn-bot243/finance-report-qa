"""三张报表主要科目获取：直连东方财富数据中心 JSON 接口（合并报表口径，单位：元）。

akshare 包的 HTML 抓取接口已失效，这里直接用其底层 datacenter API。
来源列：akshare_balance / akshare_income / akshare_cashflow。
"""
from __future__ import annotations

import sqlite3

import requests

from ..config import Company
from ..logutil import setup_logging
from .baostock_client import _upsert_api_metric

log = setup_logging(__name__)

_API = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"}

# EM 字段 → 指标 key
_BALANCE_MAP = {
    "TOTAL_ASSETS": "total_assets",
    "TOTAL_LIABILITIES": "total_liabilities",
    "TOTAL_EQUITY": "equity_total",
    "MONETARYFUNDS": "monetary_funds",
    "ACCOUNTS_RECE": "accounts_receivable",
    "INVENTORY": "inventory",
    "CURRENT_RATIO": "current_ratio",
    "DEBT_ASSET_RATIO": "asset_liability_ratio",   # 披露比率，可能已是百分点值
}
_INCOME_MAP = {
    "TOTAL_OPERATE_INCOME": "revenue",
    "OPERATE_COST": "operating_cost",
    "SALE_EXPENSE": "sales_expense",
    "MANAGE_EXPENSE": "admin_expense",
    "FINANCE_EXPENSE": "finance_expense",
    "OPERATE_PROFIT": "operating_profit",
    "TOTAL_PROFIT": "total_profit",
    "PARENT_NETPROFIT": "net_profit_parent",
    "DEDUCT_PARENT_NETPROFIT": "net_profit_deduct",
}
_CASHFLOW_MAP = {
    "NETCASH_OPERATE": "ocf",
    "NETCASH_INVEST": "icf",
    "NETCASH_FINANCE": "fcf",
}

_CACHE: dict[str, list[dict]] = {}


def _fetch_report(report_name: str, code: str) -> list[dict]:
    key = f"{report_name}:{code}"
    if key not in _CACHE:
        params = dict(sortColumns="REPORT_DATE", sortTypes="-1", pageSize=60, pageNumber=1,
                      reportName=report_name, columns="ALL",
                      filter=f'(SECURITY_CODE="{code}")')
        d = requests.get(_API, params=params, headers=_HEADERS, timeout=30).json()
        _CACHE[key] = (d.get("result") or {}).get("data") or []
    return _CACHE[key]


def _is_ratio_field(key: str) -> bool:
    return key in ("CURRENT_RATIO", "DEBT_ASSET_RATIO")


def fetch_akshare(conn: sqlite3.Connection, companies: list[Company], years: list[int]) -> dict:
    stats = {"rows": 0, "missing": []}
    for company in companies:
        code = company.code
        for report_name, col_map, source in [
            ("RPT_DMSK_FN_BALANCE", _BALANCE_MAP, "akshare_balance"),
            ("RPT_DMSK_FN_INCOME", _INCOME_MAP, "akshare_income"),
            ("RPT_DMSK_FN_CASHFLOW", _CASHFLOW_MAP, "akshare_cashflow"),
        ]:
            try:
                rows = _fetch_report(report_name, code)
            except Exception as e:
                log.warning("%s %s 拉取失败: %s", code, report_name, e)
                stats["missing"].append(f"{code} {report_name} fetch failed")
                continue
            for fy in years:
                target = f"{fy}-12-31 00:00:00"
                row = next((r for r in rows if (r.get("REPORT_DATE") or "") == target), None)
                if row is None:
                    stats["missing"].append(f"{code} {fy} {report_name} 无年报数据")
                    continue
                for field, key in col_map.items():
                    v = row.get(field)
                    if v in (None, ""):
                        continue
                    try:
                        value = float(v)
                    except (TypeError, ValueError):
                        continue
                    _upsert_api_metric(
                        conn, code, fy, key, value, str(v),
                        source=source, api_ref=f"em_datacenter:{report_name}",
                        source_text=f"东方财富数据中心 {report_name} {code} {fy}-12-31",
                    )
                    stats["rows"] += 1
    # 资产负债率派生兜底（负债合计/资产总计，缺失披露值时才补）
    conn.execute(
        """INSERT INTO metrics (code, fiscal_year, metric_key, value, raw_value, raw_unit,
                                source, doc_id, page_no, source_text, column_label,
                                method, confidence, api_ref)
           SELECT code, fiscal_year, 'asset_liability_ratio',
                  ROUND(total_liabilities.value / total_assets.value * 100, 6),
                  'derived', '%', 'akshare_balance', NULL, NULL,
                  'derived from 东方财富 负债合计/资产总计', '本期', 'api', 'medium',
                  'em_datacenter:derived'
           FROM (SELECT code, fiscal_year, value FROM metrics WHERE source='akshare_balance'
                 AND metric_key='total_liabilities' AND doc_id IS NULL) total_liabilities
           JOIN (SELECT code, fiscal_year, value FROM metrics WHERE source='akshare_balance'
                 AND metric_key='total_assets' AND doc_id IS NULL) total_assets
             USING (code, fiscal_year)
           WHERE total_assets.value != 0
           ON CONFLICT (code, fiscal_year, metric_key, source, column_label) WHERE doc_id IS NULL
           DO NOTHING""",
    )
    conn.commit()
    log.info("akshare(东财直连) 完成: %s", stats)
    return stats
