"""指标提取主编排：S1 定位第二节 → S2 取表 → S3 分类 → S4 单位 → S5 列识别
→ S6 锚定取值 → S7 正则兜底 → S8 交叉校验。幂等落库。"""
from __future__ import annotations

import json
import re
import sqlite3

from .. import paths
from ..config import Config
from ..logutil import setup_logging
from ..store import db
from .anchor import anchor_rows, parse_number, regex_fallback
from .defs import (BALANCE, CASHFLOW, INCOME, METRIC_BY_KEY, METRIC_DEFS, PROSPECTUS,
                   S2_DATA, S2_RATIO, MetricDef)
from .tabparse import ParsedTable, classify_table, clean_table, detect_columns, stitch_tables
from .units import pick_unit

log = setup_logging(__name__)

# 三大报表定位词
_STMT_PAT = {"balance_sheet": "合并资产负债表", "income_stmt": "合并利润表", "cashflow": "合并现金流量表"}
_YOY_CHANGE = "增减幅度"


def _load_pages(conn: sqlite3.Connection, doc_id: int) -> list[dict]:
    return conn.execute(
        "SELECT page_no, text, tables_json FROM pages WHERE doc_id=? ORDER BY page_no", (doc_id,)
    ).fetchall()


def _find_section2_start(pages: list[dict]) -> int | None:
    """S1：定位第二节正文首页。标题常跨两行（「第二节」/「公司简介和主要财务指标」），
    且要排除目录页（下一行是带省略号的页码行）。"""
    for p in pages:
        lines = p["text"].split("\n")
        for i, line in enumerate(lines):
            if not re.search(r"第[一二三四五六七八九十]+节", line):
                continue
            next_line = lines[i + 1] if i + 1 < len(lines) else ""
            if "公司简介和主要财务指标" in line or "公司简介和主要财务指标" in next_line:
                # 目录页：目标行或下一行带省略号
                if re.search(r"[…\.]{3,}", line) or re.search(r"[…\.]{3,}", next_line):
                    continue
                return p["page_no"]
    return None


def _find_stmt_page(pages: list[dict], kind: str) -> int | None:
    word = _STMT_PAT[kind]
    for p in pages:
        if word in p["text"]:
            return p["page_no"]
    return None


def _page_tables(pages: list[dict], page_nos: set[int]) -> list[tuple[int, list[list[str]]]]:
    """从 tables_json 还原清洗后的表格。返回 [(page_no, rows)]。"""
    out = []
    for p in pages:
        if p["page_no"] not in page_nos or not p["tables_json"]:
            continue
        try:
            tabs = json.loads(p["tables_json"])
        except json.JSONDecodeError:
            continue
        for t in tabs:
            rows = clean_table(t.get("rows") or [])
            if len(rows) >= 2:
                out.append((p["page_no"], rows))
    return out


def _normalize_value(val: float, unit_type: str) -> float:
    """按指标单位类型归一：currency 已乘倍数；percent 存百分点（值本身）；per_share 原值。"""
    return val


def _yoy_def_for(base_key: str) -> MetricDef | None:
    return next((m for m in METRIC_DEFS if m.yoy_of == base_key), None)


def _extract_from_table(
    conn: sqlite3.Connection,
    doc: dict,
    pages_by_no: dict[int, str],
    table: ParsedTable,
) -> int:
    """S4-S6：单表提取。返回提取条数。"""
    n = 0
    fiscal_year = doc["fiscal_year"]
    defs = [m for m in METRIC_DEFS if table.kind in m.tables and m.yoy_of is None]
    for mdef in defs:
        for row_i in anchor_rows(table.rows, mdef, table.kind):
            # 普通列
            if table.kind == PROSPECTUS:
                col_targets = [(int(y), y) for y in sorted(table.col_map.keys()) if y.isdigit()]
            else:
                col_targets = [(fiscal_year, "本期"), (fiscal_year - 1, "上年同期")]
            for fy, col_label in col_targets:
                ci = table.col_map.get(col_label if table.kind != PROSPECTUS else str(col_label))
                if ci is None or ci >= len(table.rows[row_i]):
                    continue
                cell = str(table.rows[row_i][ci] or "").strip()
                val = parse_number(cell)
                if val is None:
                    continue
                if mdef.unit_type == "currency":
                    val = val * table.unit_factor
                raw_unit = table.unit_word if mdef.unit_type == "currency" else (
                    "%" if mdef.unit_type in ("percent", "pp") else "元/股")
                db.upsert(conn, "metrics", {
                    "code": doc["code"], "fiscal_year": fy, "metric_key": mdef.key,
                    "value": val, "raw_value": cell, "raw_unit": raw_unit,
                    "source": table.kind, "doc_id": doc["doc_id"], "page_no": table.page_no,
                    "source_text": " | ".join(table.rows[row_i][:6])[:300],
                    "column_label": f"{col_label}" if table.kind == PROSPECTUS else col_label,
                    "method": "table",
                    "confidence": "high" if table.unit_source != "assumed" else "medium",
                    "basis": "as_reported",
                    "cross_metric_id": None, "cross_status": None,
                }, ("doc_id", "fiscal_year", "metric_key", "source", "column_label"))
                n += 1
            # 增减幅度列派生 _yoy
            if table.kind != PROSPECTUS:
                yoy_def = _yoy_def_for(mdef.key)
                if yoy_def:
                    ci = table.col_map.get(_YOY_CHANGE)
                    if ci is not None and ci < len(table.rows[row_i]):
                        cell = str(table.rows[row_i][ci] or "").strip()
                        val = parse_number(cell)
                        if val is not None:
                            db.upsert(conn, "metrics", {
                                "code": doc["code"], "fiscal_year": fiscal_year,
                                "metric_key": yoy_def.key, "value": val, "raw_value": cell,
                                "raw_unit": "%" if yoy_def.unit_type != "pp" else "百分点",
                                "source": table.kind, "doc_id": doc["doc_id"],
                                "page_no": table.page_no,
                                "source_text": " | ".join(table.rows[row_i][:6])[:300],
                                "column_label": "本期", "method": "table",
                                "confidence": "medium",
                                "basis": "as_reported",
                                "cross_metric_id": None, "cross_status": None,
                            }, ("doc_id", "fiscal_year", "metric_key", "source", "column_label"))
                            n += 1
    return n


def extract_doc_metrics(conn: sqlite3.Connection, doc: dict, force: bool = False) -> dict:
    """单文档提取。返回统计。异常时回滚本事务（不留半成品数据）。"""
    if not force:
        exists = conn.execute(
            "SELECT COUNT(*) c FROM metrics WHERE doc_id=?", (doc["doc_id"],)
        ).fetchone()["c"]
        if exists:
            return {"doc_id": doc["doc_id"], "extracted": 0, "skipped": True}
    else:
        # 自引用外键（cross_metric_id）会阻止删除，先清引用（交叉校验每次全量重跑）
        conn.execute("UPDATE metrics SET cross_metric_id=NULL, cross_status=NULL")
        conn.execute("DELETE FROM metrics WHERE doc_id=?", (doc["doc_id"],))
    try:
        return _extract_doc_metrics_inner(conn, doc)
    except Exception:
        conn.rollback()
        raise


def _extract_doc_metrics_inner(conn: sqlite3.Connection, doc: dict) -> dict:

    pages = _load_pages(conn, doc["doc_id"])
    pages_by_no = {p["page_no"]: p["text"] for p in pages}
    total = 0

    # 目标页集合
    target_pages: set[int] = set()
    s2_start = _find_section2_start(pages)
    if s2_start is not None:
        target_pages |= set(range(s2_start, min(s2_start + 8, len(pages) + 1)))
    else:
        log.warning("doc %d: 未定位第二节，全文搜「主要会计数据」", doc["doc_id"])
        for p in pages:
            if "主要会计数据" in p["text"]:
                target_pages |= set(range(max(1, p["page_no"] - 2), p["page_no"] + 6))
                break
    for kind, word in _STMT_PAT.items():
        sp = _find_stmt_page(pages, kind)
        if sp is not None:
            target_pages |= {sp, sp + 1}

    # S2-S3：取表、跨页拼接断裂表格、分类
    tables: list[ParsedTable] = []
    raw_page_tables = _page_tables(pages, target_pages)
    raw_page_tables.sort(key=lambda x: x[0])
    for page_no, rows in stitch_tables(raw_page_tables):
        kind = classify_table(rows, doc["doc_type"])
        if kind is None:
            continue
        factor, unit_word, unit_src = pick_unit(rows, pages_by_no.get(page_no, "").split("\n"))
        col_map = detect_columns(rows, kind, doc["fiscal_year"])
        tables.append(ParsedTable(page_no=page_no, rows=rows, kind=kind,
                                  col_map=col_map, unit_factor=factor,
                                  unit_word=unit_word, unit_source=unit_src))

    # 同页同类多表取行数最多
    best: dict[tuple[int, str], ParsedTable] = {}
    for t in tables:
        k = (t.page_no, t.kind)
        if k not in best or len(t.rows) > len(best[k].rows):
            best[k] = t

    # S6：提取
    for t in best.values():
        total += _extract_from_table(conn, doc, pages_by_no, t)

    # S7：正则兜底（仅第二节窗口页，只补 section2 类缺失指标）
    s2_defs = [m for m in METRIC_DEFS if S2_DATA in m.tables and m.yoy_of is None]
    have_keys = {
        (r["metric_key"], r["source"]) for r in
        conn.execute("SELECT metric_key, source FROM metrics WHERE doc_id=?", (doc["doc_id"],))
    }
    for pno in sorted(target_pages):
        text = pages_by_no.get(pno, "")
        for hit in regex_fallback(text, s2_defs, S2_DATA):
            key = (hit["metric_key"], S2_DATA)
            if key in have_keys:
                continue
            db.upsert(conn, "metrics", {
                "code": doc["code"], "fiscal_year": doc["fiscal_year"],
                "metric_key": hit["metric_key"], "value": hit["value"],
                "raw_value": hit["raw_value"], "raw_unit": None,
                "source": S2_DATA, "doc_id": doc["doc_id"], "page_no": pno,
                "source_text": hit["line"], "column_label": "本期",
                "method": "regex_fallback", "confidence": "low",
                "basis": "as_reported",
                "cross_metric_id": None, "cross_status": None,
            }, ("doc_id", "fiscal_year", "metric_key", "source", "column_label"))
            have_keys.add(key)
            total += 1

    conn.commit()
    log.info("doc %d (%s FY%d): 提取 %d 条指标", doc["doc_id"], doc["code"], doc["fiscal_year"], total)
    return {"doc_id": doc["doc_id"], "extracted": total, "skipped": False}


def _rel_err(a: float, b: float) -> float:
    if b == 0:
        return 0.0 if a == 0 else float("inf")
    return abs(a - b) / abs(b)


def cross_check_all(conn: sqlite3.Connection, cfg: Config) -> dict:
    """S8：交叉校验（同文档 section2 vs 报表；跨年 上年同期 vs 上年本期；招股书 vs 次年上年同期）。"""
    tol = float(cfg.qc.get("cross_tolerance", 0.005))
    results = {"checked": 0, "match": 0, "mismatch": 0}

    def _compare(a: dict, b: dict, label: str) -> None:
        nonlocal results
        if a["value"] is None or b["value"] is None:
            return
        err = _rel_err(a["value"], b["value"])
        if a["metric_key"].endswith("_yoy") or METRIC_BY_KEY[a["metric_key"]].unit_type in ("percent", "pp"):
            ok = abs(a["value"] - b["value"]) <= 0.1  # 百分点类绝对容差
        else:
            ok = err <= tol
        status = "match" if ok else "mismatch"
        conn.execute(
            "UPDATE metrics SET cross_metric_id=?, cross_status=? WHERE metric_id=?",
            (b["metric_id"], status, a["metric_id"]),
        )
        results["checked"] += 1
        results[status] += 1
        if not ok:
            log.warning("cross mismatch %s: %s=%.4f vs %s=%.4f (err=%.2e)",
                        label, a["source"], a["value"], b["source"], b["value"], err)

    conn.execute("UPDATE metrics SET cross_metric_id=NULL, cross_status=NULL")

    # ① 同文档：section2 vs 三大报表（本期，同 fiscal_year）
    rows = conn.execute(
        """SELECT m.* FROM metrics m WHERE m.source='section2_data' AND m.column_label='本期'
           AND m.metric_key IN ('revenue','net_profit_parent','net_profit_deduct','ocf','total_assets','net_assets_parent')
           ORDER BY m.metric_id"""
    ).fetchall()
    for a in rows:
        b = conn.execute(
            """SELECT * FROM metrics WHERE code=? AND fiscal_year=? AND metric_key=? AND source!='section2_data'
               AND column_label IN ('本期','') ORDER BY confidence DESC, metric_id LIMIT 1""",
            (a["code"], a["fiscal_year"], a["metric_key"]),
        ).fetchone()
        if b:
            _compare(a, b, f"{a['code']} FY{a['fiscal_year']} {a['metric_key']}")

    # ② 跨文档：Y年「上年同期」 vs Y-1年「本期」（同 source，且必须来自不同文档）
    rows = conn.execute(
        "SELECT * FROM metrics WHERE column_label='上年同期' ORDER BY metric_id"
    ).fetchall()
    for a in rows:
        b = conn.execute(
            """SELECT * FROM metrics WHERE code=? AND fiscal_year=? AND metric_key=? AND source=?
               AND column_label IN ('本期',?) AND doc_id!=?
               ORDER BY confidence DESC, metric_id LIMIT 1""",
            (a["code"], a["fiscal_year"], a["metric_key"], a["source"],
             f"{a['fiscal_year']}年度", a["doc_id"]),
        ).fetchone()
        if b:
            _compare(a, b, f"{a['code']} FY{a['fiscal_year']} {a['metric_key']} 上年同期")

    # ③ 招股书值 vs 次年报告上年同期列（跨 source 匹配，仅 currency 类）
    rows = conn.execute(
        """SELECT m.* FROM metrics m JOIN documents d ON m.doc_id=d.doc_id
           WHERE d.doc_type='prospectus' AND m.column_label!='本期'
             AND m.metric_key IN ('revenue','net_profit_parent','total_assets','net_assets_parent')
           ORDER BY m.metric_id"""
    ).fetchall()
    for a in rows:
        b = conn.execute(
            """SELECT * FROM metrics WHERE code=? AND fiscal_year=? AND metric_key=?
               AND column_label='上年同期' ORDER BY confidence DESC, metric_id LIMIT 1""",
            (a["code"], a["fiscal_year"], a["metric_key"]),
        ).fetchone()
        if b:
            _compare(a, b, f"{a['code']} 招股书 FY{a['fiscal_year']} {a['metric_key']}")
    conn.commit()
    log.info("交叉校验完成: %s", results)
    return results


def extract_all(cfg: Config, force: bool = False, only: str | None = None) -> dict:
    conn = db.connect()
    stats = {"docs": 0, "skipped": 0, "extracted_total": 0}
    rows = conn.execute(
        "SELECT * FROM documents WHERE download_status='ok' ORDER BY doc_id"
    ).fetchall()
    for doc in rows:
        if only and doc["code"] != only:
            continue
        try:
            r = extract_doc_metrics(conn, doc, force=force)
            if r.get("skipped"):
                stats["skipped"] += 1
            else:
                stats["docs"] += 1
                stats["extracted_total"] += r["extracted"]
        except Exception as e:
            log.exception("doc %d 提取失败", doc["doc_id"])
    cross_check_all(conn, cfg)
    from .evidence import backfill_evidence
    backfill_evidence(conn, cfg)
    conn.close()
    log.info("指标提取完成: %s", stats)
    return stats
