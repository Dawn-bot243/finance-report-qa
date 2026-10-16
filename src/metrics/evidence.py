"""出处回填：API 指标行（doc_id IS NULL）与 PDF 原文值匹配后，回填年报文档+页码出处。

- 匹配成功（容差内）→ evidence_doc_id/evidence_page_no 指向 PDF 行，cross_status='match'
- 无匹配 → 链接数值最近的 PDF 行并标 cross_status='mismatch'（供人工核对，不回填出处）
- datasinking 行与 PDF 同源（cninfo 同一文件），额外通过 report_markdown.adjunct_url
  关联文档，即使无逐值匹配也有文档级出处
"""
from __future__ import annotations

import sqlite3

from ..config import Config
from ..logutil import setup_logging
from .defs import METRIC_BY_KEY

log = setup_logging(__name__)


def _agree(a: float, b: float, unit_type: str, tol: float) -> bool:
    if unit_type in ("percent", "pp"):
        return abs(a - b) <= 0.1
    if b == 0:
        return a == 0
    return abs(a - b) / abs(b) <= tol


def backfill_evidence(conn: sqlite3.Connection, cfg: Config) -> dict:
    tol = float(cfg.qc.get("cross_tolerance", 0.005))
    api_rows = conn.execute(
        "SELECT * FROM metrics WHERE doc_id IS NULL ORDER BY metric_id"
    ).fetchall()
    stats = {"total": 0, "matched": 0, "mismatched": 0, "no_pdf_candidate": 0}
    for r in api_rows:
        stats["total"] += 1
        cands = conn.execute(
            """SELECT m.*, d.is_primary FROM metrics m
               JOIN documents d ON m.doc_id=d.doc_id
               WHERE m.code=? AND m.fiscal_year=? AND m.metric_key=?
                 AND m.doc_id IS NOT NULL AND m.column_label='本期' AND m.value IS NOT NULL
               ORDER BY d.is_primary DESC, m.confidence DESC, m.metric_id""",
            (r["code"], r["fiscal_year"], r["metric_key"]),
        ).fetchall()
        if not cands:
            stats["no_pdf_candidate"] += 1
            continue
        unit_type = METRIC_BY_KEY[r["metric_key"]].unit_type
        best = next((c for c in cands if _agree(r["value"], c["value"], unit_type, tol)), None)
        if best is not None:
            conn.execute(
                """UPDATE metrics SET cross_metric_id=?, cross_status='match',
                   evidence_doc_id=?, evidence_page_no=? WHERE metric_id=?""",
                (best["metric_id"], best["doc_id"], best["page_no"], r["metric_id"]),
            )
            stats["matched"] += 1
        else:
            closest = min(cands, key=lambda c: abs(r["value"] - c["value"]))
            conn.execute(
                """UPDATE metrics SET cross_metric_id=?, cross_status='mismatch',
                   evidence_doc_id=NULL, evidence_page_no=NULL WHERE metric_id=?""",
                (closest["metric_id"], r["metric_id"]),
            )
            stats["mismatched"] += 1

    # datasinking 行与 PDF 同源：通过 adjunct_url 关联文档（文档级出处）
    conn.execute(
        """UPDATE metrics
           SET evidence_doc_id = (SELECT doc_id FROM documents
                                  WHERE documents.adjunct_url = rm.adjunct_url
                                  LIMIT 1)
           FROM report_markdown rm
           WHERE metrics.source='datasinking' AND metrics.doc_id IS NULL
             AND metrics.evidence_doc_id IS NULL
             AND rm.code = metrics.code
             AND rm.report_period = metrics.fiscal_year || '-12-31'
             AND rm.adjunct_url IS NOT NULL""",
    )
    conn.commit()
    log.info("出处回填: %s", stats)
    return stats
