"""RAG 索引：pages（主选文档）→ 语义子块 → 稠密向量 + BM25（父子块结构）。

检索在子块上进行（更聚焦），命中后回溯到父页（整页）用于生成与【第N页】引用。

- 子块：src/rag/chunking.py 按标题/段落/表格切分，平均 ~3.7 块/页
- 稠密向量：numpy 持久化（36k×1024 float32 ≈ 150MB），进程内缓存
- 幂等：按子块 sha256 增量，只嵌入新增/变更块；旧版页级索引会被识别并要求重建
"""
from __future__ import annotations

import hashlib
import pickle
import sqlite3

import jieba
import numpy as np
from rank_bm25 import BM25Okapi

from .. import paths
from ..config import Config
from ..logutil import setup_logging
from .chunking import split_page
from .clients import embed

log = setup_logging(__name__)

DENSE_PATH = paths.dir_db() / "dense.npz"
BM25_PATH = paths.dir_db() / "bm25.pkl"
DENSE_VERSION = "child_v1"   # 子块索引格式版本

_CHILD_DDL = """
CREATE TABLE IF NOT EXISTS rag_child_chunks (
    child_id    INTEGER PRIMARY KEY,
    doc_id      INTEGER NOT NULL REFERENCES documents(doc_id),
    page_no     INTEGER NOT NULL,
    seq         INTEGER NOT NULL,
    code        TEXT NOT NULL,
    fiscal_year INTEGER NOT NULL,
    text        TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    UNIQUE (doc_id, page_no, seq)
);
CREATE INDEX IF NOT EXISTS idx_child_page ON rag_child_chunks(doc_id, page_no);
"""


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _tokenize(text: str) -> list[str]:
    return [t for t in jieba.cut(text) if t.strip()]


def _load_dense() -> dict:
    if not DENSE_PATH.exists():
        return {}
    data = dict(np.load(DENSE_PATH, allow_pickle=True))
    if str(data.get("version", "")) != DENSE_VERSION:
        log.warning("稠密索引版本不匹配（%s），将全量重建", data.get("version"))
        return {}
    return data


def build_index(cfg: Config, force: bool = False) -> dict:
    conn = sqlite3.connect(paths.db_path())
    conn.row_factory = sqlite3.Row
    conn.executescript(_CHILD_DDL)
    conn.commit()

    rows = conn.execute(
        """SELECT p.page_no, p.text, d.doc_id, d.code, d.fiscal_year
           FROM pages p JOIN documents d ON p.doc_id=d.doc_id
           WHERE d.is_primary=1 ORDER BY d.doc_id, p.page_no"""
    ).fetchall()
    log.info("主选文档页数: %d", len(rows))

    # ---- 切分子块 ----
    new_children: list[dict] = []
    for r in rows:
        for c in split_page(r["text"], r["page_no"]):
            new_children.append({
                "doc_id": r["doc_id"], "page_no": r["page_no"], "seq": c["seq"],
                "code": r["code"], "fiscal_year": r["fiscal_year"],
                "text": c["text"], "sha256": c["sha256"],
            })
    log.info("子块总数: %d（平均 %.2f 块/页）",
             len(new_children), len(new_children) / max(1, len(rows)))

    old = {(r["doc_id"], r["page_no"], r["seq"]): r["sha256"]
           for r in conn.execute(
               "SELECT doc_id, page_no, seq, sha256 FROM rag_child_chunks")}
    new_keys = {(c["doc_id"], c["page_no"], c["seq"]) for c in new_children}
    stale_keys = set(old) - new_keys
    if stale_keys:
        conn.executemany("DELETE FROM rag_child_chunks WHERE doc_id=? AND page_no=? AND seq=?",
                         list(stale_keys))
        log.info("删除过期子块 %d 条", len(stale_keys))

    to_embed = [c for c in new_children
                if force or old.get((c["doc_id"], c["page_no"], c["seq"])) != c["sha256"]]
    log.info("需嵌入子块: %d / %d", len(to_embed), len(new_children))

    # ---- 稠密向量（子块级）----
    dense = {} if force else _load_dense()
    if dense:
        key_of = {(int(d), int(p), int(s)): i for i, (d, p, s) in enumerate(
            zip(dense["doc_ids"], dense["page_nos"], dense["seqs"]))}
        kept_idx = [i for k, i in key_of.items() if k in new_keys]
        base = {k: np.asarray(v)[kept_idx] for k, v in dense.items() if k != "version"}
        base_texts = [dense["texts"][i] for i in kept_idx]
    else:
        base, base_texts = None, []

    if to_embed:
        new_vecs = np.array(embed([c["text"] for c in to_embed]), dtype=np.float32)
        parts = {
            "version": np.array(DENSE_VERSION),
            "vectors": np.concatenate([base["vectors"], new_vecs]) if base
            else new_vecs,
            "doc_ids": np.concatenate([
                base["doc_ids"] if base else np.array([], dtype=np.int64),
                np.array([c["doc_id"] for c in to_embed])]),
            "page_nos": np.concatenate([
                base["page_nos"] if base else np.array([], dtype=np.int64),
                np.array([c["page_no"] for c in to_embed])]),
            "seqs": np.concatenate([
                base["seqs"] if base else np.array([], dtype=np.int64),
                np.array([c["seq"] for c in to_embed])]),
            "codes": np.concatenate([
                base["codes"] if base else np.array([], dtype=object),
                np.array([c["code"] for c in to_embed], dtype=object)]),
            "fiscal_years": np.concatenate([
                base["fiscal_years"] if base else np.array([], dtype=np.int64),
                np.array([c["fiscal_year"] for c in to_embed])]),
            "texts": np.array(base_texts + [c["text"] for c in to_embed], dtype=object),
        }
        np.savez(DENSE_PATH, **parts)
        log.info("稠密向量已写入（新增 %d，总计 %d）", len(to_embed), len(parts["vectors"]))
    elif dense:
        log.info("稠密向量无需更新（%d 条已存在）", len(dense["vectors"]))
    else:
        raise RuntimeError("索引为空且没有可嵌入的子块")

    # ---- 子块表 ----
    for c in to_embed:
        conn.execute(
            """INSERT INTO rag_child_chunks
                   (doc_id, page_no, seq, code, fiscal_year, text, sha256)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT (doc_id, page_no, seq) DO UPDATE SET
                   text=excluded.text, sha256=excluded.sha256""",
            (c["doc_id"], c["page_no"], c["seq"], c["code"], c["fiscal_year"],
             c["text"], c["sha256"]))
    conn.commit()

    # ---- BM25（子块级，全量重建）----
    all_children = conn.execute(
        "SELECT child_id, doc_id, page_no, seq, code, fiscal_year, text "
        "FROM rag_child_chunks ORDER BY child_id").fetchall()
    tokenized = [_tokenize(r["text"]) for r in all_children]
    bm25 = BM25Okapi(tokenized)
    bm25_meta = {
        "child_ids": [r["child_id"] for r in all_children],
        "doc_ids": [r["doc_id"] for r in all_children],
        "page_nos": [r["page_no"] for r in all_children],
        "seqs": [r["seq"] for r in all_children],
        "codes": [r["code"] for r in all_children],
        "fiscal_years": [r["fiscal_year"] for r in all_children],
        "texts": [r["text"] for r in all_children],
    }
    with open(BM25_PATH, "wb") as f:
        pickle.dump({"bm25": bm25, "meta": bm25_meta}, f)
    log.info("BM25 已建 %d 子块 → %s", len(all_children), BM25_PATH.name)

    conn.close()
    return {"pages": len(rows), "children": len(all_children),
            "embedded": len(to_embed), "dense": str(DENSE_PATH)}


def load_bm25() -> tuple[BM25Okapi, dict]:
    with open(BM25_PATH, "rb") as f:
        data = pickle.load(f)
    return data["bm25"], data["meta"]
