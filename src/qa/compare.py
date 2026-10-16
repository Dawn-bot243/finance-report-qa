"""质量对比：PDF 提取 vs API 来源（baostock/akshare/datasinking）。

对比维度：
1. 覆盖率：各来源对「核心指标 × 公司×财年」的覆盖条数
2. 一致性：PDF 值与 API 值（重叠指标）的相对误差分布 → PDF 提取准确度
3. API 间互验：baostock vs akshare 重叠指标一致性（双方都是 API，应高度一致）
"""
from __future__ import annotations

import sqlite3

from ..config import Config
from ..logutil import setup_logging
from ..metrics.defs import METRIC_BY_KEY

log = setup_logging(__name__)

# 对比用的核心指标（PDF 与 API 都有意义的重叠集）
CORE = ["revenue", "net_profit", "net_profit_parent", "roe_weighted", "total_assets",
        "net_assets_parent", "total_liabilities", "asset_liability_ratio",
        "eps_basic", "eps_diluted", "ocf", "icf", "fcf",
        "current_assets", "current_liabilities", "noncurrent_liabilities",
        "monetary_funds", "operating_cost", "operating_profit", "total_profit"]

PDF_SOURCES = ("section2_data", "income_stmt", "balance_sheet", "cashflow", "prospectus")
API_SOURCES = ("baostock", "akshare_balance", "akshare_income", "akshare_cashflow", "datasinking")


def _rel_err(a: float, b: float) -> float:
    if b == 0:
        return 0.0 if a == 0 else float("inf")
    return abs(a - b) / abs(b)


def _pdf_value(conn: sqlite3.Connection, code: str, fy: int, key: str) -> float | None:
    """PDF 来源优先取 confidence 最高的本期值。"""
    r = conn.execute(
        """SELECT value FROM metrics WHERE code=? AND fiscal_year=? AND metric_key=?
           AND source IN ('section2_data','income_stmt','balance_sheet','cashflow','prospectus')
           AND column_label='本期' AND value IS NOT NULL
           ORDER BY confidence DESC, metric_id LIMIT 1""",
        (code, fy, key),
    ).fetchone()
    return float(r["value"]) if r else None


def _api_value(conn: sqlite3.Connection, code: str, fy: int, key: str, sources: tuple) -> float | None:
    q = ",".join("?" * len(sources))
    r = conn.execute(
        f"""SELECT value, source FROM metrics WHERE code=? AND fiscal_year=? AND metric_key=?
            AND source IN ({q}) AND value IS NOT NULL ORDER BY metric_id LIMIT 1""",
        (code, fy, key, *sources),
    ).fetchone()
    return float(r["value"]) if r else None


def run_comparison(conn: sqlite3.Connection, cfg: Config) -> dict:
    tol = float(cfg.qc.get("cross_tolerance", 0.005))
    codes = [c.code for c in cfg.companies]
    years = cfg.fiscal_years

    # 1) 覆盖率
    pdf_hits = api_hits = 0
    total_cells = len(codes) * len(years) * len(CORE)
    pdf_covered = []
    api_covered = []
    for code in codes:
        for fy in years:
            for key in CORE:
                if _pdf_value(conn, code, fy, key) is not None:
                    pdf_hits += 1
                    pdf_covered.append((code, fy, key))
                if _api_value(conn, code, fy, key, API_SOURCES) is not None:
                    api_hits += 1
                    api_covered.append((code, fy, key))

    # 2) PDF vs API 一致性（重叠指标）
    agree = disagree = 0
    disagreements = []
    pdf_only = api_only = 0
    for code in codes:
        for fy in years:
            for key in CORE:
                pv = _pdf_value(conn, code, fy, key)
                av = _api_value(conn, code, fy, key, API_SOURCES)
                if pv is None and av is None:
                    continue
                if pv is None:
                    api_only += 1
                    continue
                if av is None:
                    pdf_only += 1
                    continue
                err = _rel_err(pv, av)
                is_pct = METRIC_BY_KEY[key].unit_type in ("percent", "pp")
                ok = abs(pv - av) <= 0.15 if is_pct else err <= tol
                if ok:
                    agree += 1
                else:
                    disagree += 1
                    disagreements.append((code, fy, key, pv, av, err))

    # 3) API 间互验（baostock vs akshare，重叠指标）
    bs_ak_agree = bs_ak_disagree = 0
    for code in codes:
        for fy in years:
            for key in CORE:
                bv = _api_value(conn, code, fy, key, ("baostock",))
                av = _api_value(conn, code, fy, key, ("akshare_balance", "akshare_income", "akshare_cashflow"))
                if bv is None or av is None:
                    continue
                is_pct = METRIC_BY_KEY[key].unit_type in ("percent", "pp")
                ok = abs(bv - av) <= 0.15 if is_pct else _rel_err(bv, av) <= tol
                bs_ak_agree += 1 if ok else 0
                bs_ak_disagree += 0 if ok else 1

    # 4) datasinking（as_reported）vs PDF 同源校验：应高度一致（同一份年报文件）
    ds_agree = ds_disagree = 0
    for code in codes:
        for fy in years:
            for key in CORE:
                dv = _api_value(conn, code, fy, key, ("datasinking",))
                pv = _pdf_value(conn, code, fy, key)
                if dv is None or pv is None:
                    continue
                is_pct = METRIC_BY_KEY[key].unit_type in ("percent", "pp")
                ok = abs(dv - pv) <= 0.15 if is_pct else _rel_err(dv, pv) <= tol
                ds_agree += 1 if ok else 0
                ds_disagree += 0 if ok else 1

    # 5) 口径维度 + 出处回填统计
    total_api_rows = conn.execute("SELECT COUNT(*) c FROM metrics WHERE doc_id IS NULL").fetchone()["c"]
    ev_match = conn.execute(
        "SELECT COUNT(*) c FROM metrics WHERE doc_id IS NULL AND evidence_doc_id IS NOT NULL"
    ).fetchone()["c"]
    ev_mismatch = conn.execute(
        "SELECT COUNT(*) c FROM metrics WHERE doc_id IS NULL AND cross_status='mismatch'"
    ).fetchone()["c"]
    restated_rows = conn.execute(
        "SELECT COUNT(*) c FROM metrics WHERE basis='restated'"
    ).fetchone()["c"]
    as_reported_rows = conn.execute(
        "SELECT COUNT(*) c FROM metrics WHERE basis='as_reported'"
    ).fetchone()["c"]

    result = {
        "total_cells": total_cells,
        "pdf_coverage_rate": round(pdf_hits / total_cells, 4),
        "api_coverage_rate": round(api_hits / total_cells, 4),
        "pdf_api_agree": agree, "pdf_api_disagree": disagree,
        "pdf_api_agreement_rate": round(agree / (agree + disagree), 4) if agree + disagree else None,
        "pdf_only_count": pdf_only, "api_only_count": api_only,
        "disagreements": disagreements[:50],
        "baostock_vs_akshare_agree": bs_ak_agree,
        "baostock_vs_akshare_disagree": bs_ak_disagree,
        "baostock_vs_akshare_rate": round(bs_ak_agree / (bs_ak_agree + bs_ak_disagree), 4)
        if bs_ak_agree + bs_ak_disagree else None,
        "datasinking_vs_pdf_agree": ds_agree,
        "datasinking_vs_pdf_disagree": ds_disagree,
        "datasinking_vs_pdf_rate": round(ds_agree / (ds_agree + ds_disagree), 4)
        if ds_agree + ds_disagree else None,
        "basis_restated_rows": restated_rows,
        "basis_as_reported_rows": as_reported_rows,
        "api_rows_total": total_api_rows,
        "evidence_backfilled": ev_match,
        "evidence_mismatch": ev_mismatch,
        "evidence_backfill_rate": round(ev_match / total_api_rows, 4) if total_api_rows else None,
    }
    log.info("对比结果: PDF覆盖率=%.2f%% API覆盖率=%.2f%% PDF-API一致率=%s",
             result["pdf_coverage_rate"] * 100, result["api_coverage_rate"] * 100,
             result["pdf_api_agreement_rate"])
    return result
