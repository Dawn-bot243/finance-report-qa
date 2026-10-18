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


def _norm_name(s: str) -> str:
    """公司名归一：全角→半角、小写、去空白。"""
    out = []
    for ch in s.strip():
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return re.sub(r"\s+", "", "".join(out)).lower()


def name_matches(config_name: str, dataset_name: str) -> bool:
    """数据集标注公司名与代码所属公司是否一致。
    大小写/全半角差异视为一致（伊泰B股/伊泰b股）；不同公司视为数据集噪声。"""
    a, b = _norm_name(config_name), _norm_name(dataset_name)
    return bool(a and b) and (a == b or a in b or b in a)


def import_cfqa(cfg: Config, force: bool = False) -> dict:
    """导入前5家公司的 CFQA 问答。返回 {rows, companies}。"""
    conn = db.connect()
    db.init_schema(conn)
    # 迁移：老库补 name_ok 列
    cols = [r[1] for r in conn.execute("PRAGMA table_info(cfqa_questions)")]
    if "name_ok" not in cols:
        conn.execute("ALTER TABLE cfqa_questions ADD COLUMN name_ok INTEGER NOT NULL DEFAULT 1")
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
            name_ok = 1 if name_matches(codes[code], name) else 0
            if not name_ok:
                log.warning("数据集噪声（公司名与代码不符）: %s 标注=%s 代码=%s",
                            r["问题"][:30], name, code)
            qid = r["id"]
            if not force and conn.execute(
                "SELECT 1 FROM cfqa_questions WHERE question_id=?", (qid,)
            ).fetchone():
                # 已有行：更新名称校验结果；噪声题标记 skip（不参与 docmap/评估）
                conn.execute(
                    "UPDATE cfqa_questions SET name_ok=?, qa_check=CASE WHEN ?=0 THEN 'skip' ELSE qa_check END "
                    "WHERE question_id=?",
                    (name_ok, name_ok, qid),
                )
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
                "name_ok": name_ok,
                "qa_doc_map": None,
                "qa_check": None if name_ok else "skip",
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
