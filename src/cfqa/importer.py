"""CFQA 数据集导入：三个 split JSON → companies/cfqa_questions（按配置公司过滤、名称核对）。"""
from __future__ import annotations

import json
import re
import sqlite3

from .. import paths
from ..config import Config
from ..logutil import setup_logging
from ..store import db

log = setup_logging(__name__)

_YEAR_PAT = re.compile(r"20\d{2}")


def _parse_q_year(question: str) -> int | None:
    m = _YEAR_PAT.search(question)
    return int(m.group(0)) if m else None


def import_cfqa(cfg: Config, force: bool = False) -> dict:
    """导入前5家公司的 CFQA 问答。返回 {rows, companies}。"""
    conn = db.connect()
    db.init_schema(conn)
    # companies 先用 config 写入（org_id 由 download 阶段反查后回填）
    codes = {c.code: c.name for c in cfg.companies}
    if not codes:
        raise RuntimeError("config 未配置公司")
    for c in cfg.companies:
        db.upsert(conn, "companies", {
            "code": c.code, "name": c.name, "org_id": c.org_id,
            "market": c.market, "listed_date": c.listed_date, "note": c.note,
        }, ("code",))

    total = 0
    split_files = [
        ("train", paths.dir_raw() / "cfqa" / "split_by_company_train.json"),
        ("dev", paths.dir_raw() / "cfqa" / "split_by_company_dev.json"),
        ("test", paths.dir_raw() / "cfqa" / "split_by_company_test.json"),
    ]
    for split, path in split_files:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        rows = data if isinstance(data, list) else data["data"]
        for r in rows:
            code = str(r["股票代码"])
            if code not in codes:
                continue
            name = r["公司"]
            if name not in codes[code]:
                log.warning("公司名不一致: 数据集=%s(%s) config=%s", name, code, codes[code])
            qid = r["id"]
            if not force and conn.execute(
                "SELECT 1 FROM cfqa_questions WHERE question_id=?", (qid,)
            ).fetchone():
                continue
            db.upsert(conn, "cfqa_questions", {
                "question_id": qid,
                "code": code,
                "company_name": name,
                "split": split,
                "question": r["问题"],
                "answer": r["答案"],
                "answer_pages": json.dumps(r["答案出自"], ensure_ascii=False),
                "q_year": _parse_q_year(r["问题"]),
                "qa_doc_map": None,
                "qa_check": None,
            }, ("question_id",))
            total += 1
    conn.commit()
    n = conn.execute(
        "SELECT COUNT(*) c FROM cfqa_questions WHERE code IN (%s)"
        % ",".join("?" * len(codes)), list(codes.keys())
    ).fetchone()["c"]
    conn.close()
    log.info("CFQA 导入完成：本次写入 %d 条，库内前5家共 %d 条", total, n)
    return {"rows": total, "total_in_db": n}
