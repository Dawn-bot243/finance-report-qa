"""API 数据导入编排：baostock 指标 + akshare 三张表全科目 + datasinking Markdown/指标。"""
from __future__ import annotations

from ..config import Config
from ..logutil import setup_logging
from ..store import db
from .akshare_client import fetch_akshare
from .baostock_client import fetch_baostock
from .datasinking_client import fetch_datasinking

log = setup_logging(__name__)


def import_all_apis(cfg: Config, force: bool = False, only: str | None = None) -> dict:
    conn = db.connect()
    db.init_schema(conn)
    companies = cfg.companies if not only else [c for c in cfg.companies if c.code == only]
    if not companies:
        raise RuntimeError("config 未配置公司")
    results = {}
    for name, fn in [("baostock", fetch_baostock), ("akshare", fetch_akshare),
                     ("datasinking", fetch_datasinking)]:
        try:
            results[name] = fn(conn, companies, cfg.fiscal_years)
        except Exception as e:
            log.exception("%s 导入失败", name)
            results[name] = {"error": str(e)}
    # 出处回填：API 行与 PDF 原文值匹配后关联年报文档+页码
    from ..metrics.evidence import backfill_evidence
    try:
        results["evidence"] = backfill_evidence(conn, cfg)
    except Exception as e:
        log.exception("出处回填失败")
        results["evidence"] = {"error": str(e)}
    conn.close()
    log.info("API 导入完成: %s", results)
    return results
