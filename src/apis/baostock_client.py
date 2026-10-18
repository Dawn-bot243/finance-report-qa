"""baostock 指标数据获取：盈利能力/偿债/现金流/成长/营运/杜邦六组指标。

字段→指标映射（比率字段 baostock 为小数，入库统一 ×100 存百分点）：
- query_profit_data: MBRevenue→revenue, netProfit→net_profit, roeAvg→roe_weighted,
  gpMargin→gross_margin, npMargin→net_margin, epsTTM→eps_ttm, totalShare→total_share, liqaShare→liqa_share
- query_balance_data: liabilityToAsset→asset_liability_ratio, currentRatio/quickRatio/cashRatio→对应比率,
  assetToEquity→equity_multiplier
- query_growth_data: YOYNI→yoy_net_profit, YOYPNI→yoy_net_profit_parent, YOYEPSBasic→yoy_eps_basic,
  YOYAsset→yoy_total_assets, YOYEquity→yoy_equity
- query_operation_data: AssetTurnRatio→asset_turnover
- query_cash_flow_data: CFOToNP→cfo_to_np
- query_dupont_data: dupontROE→roe_dupont
"""
from __future__ import annotations

import sqlite3

import baostock as bs

from ..config import Company
from ..logutil import setup_logging

log = setup_logging(__name__)

# (API 函数名, 字段, 指标key, 单位类型, 换算)
_FIELD_MAP: dict[str, dict[str, tuple[str, str, float]]] = {
    "query_profit_data": {
        "MBRevenue": ("revenue", "currency", 1.0),
        "netProfit": ("net_profit", "currency", 1.0),
        "roeAvg": ("roe_weighted", "percent", 100.0),
        "gpMargin": ("gross_margin", "percent", 100.0),
        "npMargin": ("net_margin", "percent", 100.0),
        "epsTTM": ("eps_ttm", "per_share", 1.0),
        "totalShare": ("total_share", "share", 1.0),
        "liqaShare": ("liqa_share", "share", 1.0),
    },
    "query_balance_data": {
        "liabilityToAsset": ("asset_liability_ratio", "percent", 100.0),
        "currentRatio": ("current_ratio", "ratio", 1.0),
        "quickRatio": ("quick_ratio", "ratio", 1.0),
        "cashRatio": ("cash_ratio", "ratio", 1.0),
        "assetToEquity": ("equity_multiplier", "ratio", 1.0),
    },
    "query_growth_data": {
        "YOYNI": ("yoy_net_profit", "percent", 100.0),
        "YOYPNI": ("yoy_net_profit_parent", "percent", 100.0),
        "YOYEPSBasic": ("yoy_eps_basic", "percent", 100.0),
        "YOYAsset": ("yoy_total_assets", "percent", 100.0),
        "YOYEquity": ("yoy_equity", "percent", 100.0),
    },
    "query_operation_data": {
        "AssetTurnRatio": ("asset_turnover", "ratio", 1.0),
    },
    "query_cash_flow_data": {
        "CFOToNP": ("cfo_to_np", "ratio", 1.0),
    },
    "query_dupont_data": {
        "dupontROE": ("roe_dupont", "percent", 100.0),
    },
}

def bs_code(code: str) -> str:
    """A/B 股代码 → baostock 代码：6/9 开头为上交所，其余深交所。"""
    return ("sh." if code.startswith(("6", "9")) else "sz.") + code


def _num(v: str) -> float | None:
    try:
        return float(v) if v not in ("", None) else None
    except ValueError:
        return None


def _upsert_api_metric(conn: sqlite3.Connection, code: str, fy: int, key: str, value: float,
                       raw: str, source: str, api_ref: str, source_text: str,
                       basis: str = "restated") -> None:
    """API 指标行（doc_id 为 NULL）按部分唯一索引幂等 upsert。

    basis: restated（baostock/东财等追溯重述值，默认）| as_reported（datasinking，同源年报原文）
    """
    conn.execute(
        """INSERT INTO metrics (code, fiscal_year, metric_key, value, raw_value, raw_unit,
                                source, doc_id, page_no, source_text, column_label,
                                method, confidence, api_ref, basis)
           VALUES (?,?,?,?,?,?,?,NULL,NULL,?,'本期','api','high',?,?)
           ON CONFLICT (code, fiscal_year, metric_key, source, column_label) WHERE doc_id IS NULL
           DO UPDATE SET value=excluded.value, raw_value=excluded.raw_value,
                         raw_unit=excluded.raw_unit, source_text=excluded.source_text,
                         api_ref=excluded.api_ref, basis=excluded.basis,
                         extracted_at=datetime('now','localtime')""",
        (code, fy, key, value, raw, None, source, source_text, api_ref, basis),
    )


def fetch_baostock(conn: sqlite3.Connection, companies: list[Company], years: list[int]) -> dict:
    """全量拉取 baostock 指标。返回统计。"""
    stats = {"rows": 0, "queries": 0, "empty": 0}
    lg = bs.login()
    if lg.error_code != "0":
        raise RuntimeError(f"baostock 登录失败: {lg.error_msg}")
    try:
        for company in companies:
            code_bs = bs_code(company.code)
            for fy in years:
                for api_name, field_map in _FIELD_MAP.items():
                    rs = getattr(bs, api_name)(code=code_bs, year=fy, quarter=4)
                    rows = []
                    while rs.next():
                        rows.append(rs.get_row_data())
                    stats["queries"] += 1
                    if not rows:
                        stats["empty"] += 1
                        continue
                    row = rows[0]
                    fields = rs.fields
                    pub_date = row[fields.index("pubDate")] if "pubDate" in fields else ""
                    for field, (key, _unit, scale) in field_map.items():
                        if field not in fields:
                            continue
                        v = _num(row[fields.index(field)])
                        if v is None:
                            continue
                        value = round(v * scale, 8)
                        _upsert_api_metric(
                            conn, company.code, fy, key, value, row[fields.index(field)],
                            source="baostock", api_ref=f"baostock:{api_name}",
                            source_text=f"baostock {api_name} {bs_code} {fy}年报 pubDate={pub_date}",
                        )
                        stats["rows"] += 1
    finally:
        bs.logout()
    conn.commit()
    log.info("baostock 完成: %s", stats)
    return stats
