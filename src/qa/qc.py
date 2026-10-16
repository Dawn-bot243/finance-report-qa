"""四道质量门：Gate1 文档完整性 / Gate2 答案命中 / Gate3 指标交叉 / Gate4 提取率。"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter

from ..config import Config
from ..logutil import setup_logging
from ..store import db
from .docmap import map_questions, set_primary_by_hitrate

log = setup_logging(__name__)


def _record_gate(conn: sqlite3.Connection, gate: str, passed: bool, detail: dict) -> None:
    conn.execute("INSERT INTO qc_runs (gate, pass, detail_json) VALUES (?,?,?)",
                 (gate, 1 if passed else 0, json.dumps(detail, ensure_ascii=False, default=str)))


def gate1_documents(conn: sqlite3.Connection, cfg: Config) -> tuple[bool, dict]:
    """文档完整性 + CFQA 页码 ≤ 文档页数。"""
    problems = []
    docs = conn.execute("SELECT * FROM documents").fetchall()
    if len(docs) < 20:
        problems.append(f"文档总数 {len(docs)} < 20")
    sha_dup = Counter(d["sha256"] for d in docs)
    if any(c > 1 for c in sha_dup.values()):
        problems.append(f"sha256 重复: {[k for k, c in sha_dup.items() if c > 1]}")
    for d in docs:
        if d["download_status"] != "ok":
            problems.append(f"doc {d['doc_id']} 下载状态 {d['download_status']}")
        if d["page_count"] and not (50 <= d["page_count"] <= 900):
            problems.append(f"doc {d['doc_id']} 页数异常 {d['page_count']}")
        if not d["page_count"]:
            problems.append(f"doc {d['doc_id']} 未文本化")
    # 页码校验（docmap 后）
    page_out = 0
    for q in conn.execute("SELECT * FROM cfqa_questions WHERE qa_check!='skip'").fetchall():
        if not q["qa_doc_map"]:
            continue
        m = json.loads(q["qa_doc_map"])
        doc = conn.execute("SELECT page_count FROM documents WHERE doc_id=?", (m["doc_id"],)).fetchone()
        if not doc:
            continue
        for g in m.get("groups", []):
            for pg in g.get("pages", []):
                if pg > (doc["page_count"] or 0):
                    page_out += 1
    if page_out:
        problems.append(f"{page_out} 个答案页码超出文档页数")
    detail = {"problems": problems}
    return not problems, detail


def gate2_answers(conn: sqlite3.Connection, cfg: Config) -> tuple[bool, dict]:
    """答案文本命中率（按公司×年份），目标 ≥85%（skip 条不计分母）。"""
    rows = conn.execute("SELECT * FROM cfqa_questions").fetchall()
    by_group: dict[tuple, list[str]] = {}
    for q in rows:
        by_group.setdefault((q["code"], q["q_year"]), []).append(q["qa_check"])
    hit = {g: sum(1 for s in v if s in ("ok", "partial"))
           for g, v in by_group.items()}
    valid = {g: sum(1 for s in v if s != "skip") for g, v in by_group.items()}
    rates = {g: (hit[g] / valid[g] if valid[g] else None) for g in by_group}
    total = sum(valid.values())
    total_ok = sum(hit.values())
    overall = total_ok / total if total else 0.0
    target = float(cfg.qc.get("gate2_hit_rate_target", 0.85))
    passed = overall >= target
    detail = {
        "overall_hit_rate": round(overall, 4), "target": target,
        "total_valid": total, "total_hit": total_ok,
        "per_company_year": {f"{c} FY{y}": (round(rates[(c, y)], 4) if rates[(c, y)] is not None else None)
                             for (c, y), r in sorted(rates.items())},
        "skip_empty_pages": sum(1 for q in rows if q["qa_check"] == "skip"),
    }
    return passed, detail


def gate3_cross(conn: sqlite3.Connection, cfg: Config) -> tuple[bool, dict]:
    """交叉校验：mismatch 全量列报告；mismatch 占比 ≤20% 通过。"""
    rows = conn.execute(
        "SELECT * FROM metrics WHERE cross_status IS NOT NULL"
    ).fetchall()
    mismatch = [dict(r) for r in rows if r["cross_status"] == "mismatch"]
    checked = len(rows)
    rate = len(mismatch) / checked if checked else 0.0
    passed = rate <= 0.20
    return passed, {"checked": checked, "mismatch_count": len(mismatch),
                    "mismatch_rate": round(rate, 4), "mismatches": mismatch}


def gate4_extraction(conn: sqlite3.Connection, cfg: Config) -> tuple[bool, dict]:
    """提取率矩阵：核心指标 × 公司×财年。"""
    from ..metrics.defs import METRIC_BY_KEY, S2_DATA, S2_RATIO, BALANCE, INCOME, CASHFLOW
    s2_core = ["revenue", "net_profit_parent", "net_profit_deduct", "ocf", "total_assets",
               "net_assets_parent", "eps_basic", "eps_diluted", "roe_weighted", "bps",
               "revenue_yoy", "net_profit_parent_yoy"]
    stmt_core = ["total_assets", "total_liabilities", "net_assets_parent", "equity_total",
                 "revenue", "net_profit", "net_profit_parent", "ocf", "icf", "fcf",
                 "current_assets", "current_liabilities", "noncurrent_liabilities",
                 "operating_cost", "operating_profit", "total_profit"]
    matrix = {}
    missing: list[str] = []
    docs = conn.execute(
        "SELECT * FROM documents WHERE is_primary=1 ORDER BY code, fiscal_year"
    ).fetchall()
    for d in docs:
        for key in sorted(set(s2_core + stmt_core)):
            row = conn.execute(
                """SELECT m.* FROM metrics m WHERE m.doc_id=? AND m.metric_key=?
                   AND m.column_label IN ('本期','') AND m.fiscal_year=?
                   ORDER BY m.confidence DESC LIMIT 1""",
                (d["doc_id"], key, d["fiscal_year"]),
            ).fetchone()
            matrix[(d["code"], d["fiscal_year"], key)] = bool(row)
            if not row:
                missing.append(f"{d['code']} FY{d['fiscal_year']} {key}")
    def rate(core_keys):
        cells = [k for k in matrix if k[2] in core_keys]
        return sum(1 for k in cells if matrix[k]) / len(cells) if cells else 0.0
    s2_rate = rate(s2_core)
    stmt_rate = rate(stmt_core)
    t2 = float(cfg.qc.get("gate4_section2_target", 0.90))
    t3 = float(cfg.qc.get("gate4_stmt_target", 0.85))
    passed = s2_rate >= t2 and stmt_rate >= t3
    return passed, {
        "s2_rate": round(s2_rate, 4), "s2_target": t2,
        "stmt_rate": round(stmt_rate, 4), "stmt_target": t3,
        "missing_count": len(missing), "missing": missing,
        "matrix": {f"{c}|{y}|{k}": v for (c, y, k), v in matrix.items()},
    }


def run_all_gates(cfg: Config, sample: int = 20) -> dict:
    conn = db.connect()
    results: dict[str, tuple[bool, dict]] = {}
    # docmap 先跑（Gate1/2 的前置）
    map_questions(conn, cfg)
    set_primary_by_hitrate(conn, cfg)

    for name, fn in [("gate1", gate1_documents), ("gate2", gate2_answers),
                     ("gate3", gate3_cross), ("gate4", gate4_extraction)]:
        passed, detail = fn(conn, cfg)
        _record_gate(conn, name, passed, detail)
        results[name] = (passed, detail)
        log.info("%s: %s", name, "PASS" if passed else "FAIL")
    # 质量对比：PDF 提取 vs API 来源
    from .compare import run_comparison
    comparison = run_comparison(conn, cfg)
    _record_gate(conn, "compare", True, comparison)
    results["compare"] = (True, comparison)
    conn.commit()
    from .report import write_report
    report_path = write_report(conn, results)
    conn.close()
    log.info("质检报告: %s", report_path)
    return results
