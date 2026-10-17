"""RAG 索引：pages（主选文档）→ chunks 表 + numpy 稠密向量 + jieba/BM25 稀疏索引。

稠密向量用 numpy 存储（4,934×1024 float32 ≈ 20MB，进程内缓存，检索毫秒级），
元数据过滤用掩码实现。幂等：按页文本 sha256 增量，只嵌入新增/变更页。
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
from .clients import embed

log = setup_logging(__name__)

DENSE_PATH = paths.dir_db() / "dense.npz"
BM25_PATH = paths.dir_db() / "bm25.pkl"

_RAG_CHUNKS_DDL = """
CREATE TABLE IF NOT EXISTS rag_chunks (
    chunk_id    INTEGER PRIMARY KEY,
    doc_id      INTEGER NOT NULL REFERENCES documents(doc_id),
    page_no     INTEGER NOT NULL,
    code        TEXT NOT NULL,
    fiscal_year INTEGER NOT NULL,
    text        TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    UNIQUE (doc_id, page_no)
);
"""


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _tokenize(text: str) -> list[str]:
    return [t for t in jieba.cut(text) if t.strip()]


def build_index(cfg: Config, force: bool = False) -> dict:
    conn = sqlite3.connect(paths.db_path())
    conn.row_factory = sqlite3.Row
    conn.execute(_RAG_CHUNKS_DDL)
    conn.commit()

    rows = conn.execute(
        """SELECT p.page_no, p.text, d.doc_id, d.code, d.fiscal_year, d.title
           FROM pages p JOIN documents d ON p.doc_id=d.doc_id
           WHERE d.is_primary=1 ORDER BY d.doc_id, p.page_no"""
    ).fetchall()
    log.info("主选文档页数: %d", len(rows))

    # 增量判定
    to_embed: list[dict] = []
    for r in rows:
        h = _sha256(r["text"])
        old = conn.execute(
            "SELECT sha256, chunk_id FROM rag_chunks WHERE doc_id=? AND page_no=?",
            (r["doc_id"], r["page_no"]),
        ).fetchone()
        if old and old["sha256"] == h and not force:
            continue
        to_embed.append({
            "doc_id": r["doc_id"], "page_no": r["page_no"], "code": r["code"],
            "fiscal_year": r["fiscal_year"], "text": r["text"], "sha256": h,
        })
    log.info("需嵌入: %d / 共 %d", len(to_embed), len(rows))

    # 稠密向量：numpy 持久化 + 元数据对齐数组
    # 加载既有向量（存在且未 force）
    dense_meta: dict = {}
    if DENSE_PATH.exists() and not force:
        try:
            dense_meta = dict(np.load(DENSE_PATH, allow_pickle=True))
        except Exception:
            dense_meta = {}
    have_ids = {(int(d), int(p)) for d, p in
                zip(dense_meta.get("doc_ids", []), dense_meta.get("page_nos", []))}
    all_rows = [{
        "doc_id": r["doc_id"], "page_no": r["page_no"], "code": r["code"],
        "fiscal_year": r["fiscal_year"], "text": r["text"], "sha256": _sha256(r["text"]),
    } for r in rows]
    if not dense_meta:
        # 稠密文件不存在：与 rag_chunks 增量解耦，必须全量嵌入
        need = all_rows
    else:
        need = [r for r in to_embed if (r["doc_id"], r["page_no"]) not in have_ids]
        # 向量缺失过多 → 全量重嵌
        if need and len(have_ids) < len(rows) * 0.9:
            need = all_rows
    if need:
        new_vecs = np.array(embed([r["text"] for r in need]), dtype=np.float32)
        if dense_meta and len(have_ids) >= len(rows) * 0.9:
            # 增量：过滤掉重复键后拼接
            keep = [i for i, (d, p) in enumerate(
                zip(dense_meta["doc_ids"], dense_meta["page_nos"]))
                if (int(d), int(p)) not in {(r["doc_id"], r["page_no"]) for r in need}]
            base = {k: np.asarray(v) for k, v in dense_meta.items()}
            parts = {
                "vectors": np.concatenate([base["vectors"][keep], new_vecs]),
                "doc_ids": np.concatenate([np.asarray(base["doc_ids"])[keep],
                                           np.array([r["doc_id"] for r in need])]),
                "page_nos": np.concatenate([np.asarray(base["page_nos"])[keep],
                                            np.array([r["page_no"] for r in need])]),
                "codes": np.concatenate([np.asarray(base["codes"])[keep],
                                         np.array([r["code"] for r in need])]),
                "fiscal_years": np.concatenate([np.asarray(base["fiscal_years"])[keep],
                                                np.array([r["fiscal_year"] for r in need])]),
                "chunk_ids": np.concatenate([np.asarray(base["chunk_ids"])[keep],
                                             np.array([r["doc_id"] * 100000 + r["page_no"] for r in need])]),
                "texts": np.array((base["texts"][keep]).tolist() + [r["text"] for r in need], dtype=object),
            }
        else:
            parts = {
                "vectors": new_vecs,
                "doc_ids": np.array([r["doc_id"] for r in need]),
                "page_nos": np.array([r["page_no"] for r in need]),
                "codes": np.array([r["code"] for r in need]),
                "fiscal_years": np.array([r["fiscal_year"] for r in need]),
                "chunk_ids": np.array([r["doc_id"] * 100000 + r["page_no"] for r in need]),
                "texts": np.array([r["text"] for r in need], dtype=object),
            }
        np.savez(DENSE_PATH, **parts)
        log.info("稠密向量已写入 %d 条", len(need))
    else:
        log.info("稠密向量无需更新（%d 条已存在）", len(have_ids))

    # rag_chunks 表
    for r in to_embed:
        conn.execute(
            """INSERT INTO rag_chunks (doc_id, page_no, code, fiscal_year, text, sha256)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT (doc_id, page_no) DO UPDATE SET text=excluded.text, sha256=excluded.sha256""",
            (r["doc_id"], r["page_no"], r["code"], r["fiscal_year"], r["text"], r["sha256"]),
        )
    conn.commit()

    # BM25（全量重建，语料与 chunks 同源）
    all_chunks = conn.execute(
        "SELECT chunk_id, doc_id, page_no, code, fiscal_year, text FROM rag_chunks ORDER BY chunk_id"
    ).fetchall()
    tokenized = [_tokenize(r["text"]) for r in all_chunks]
    bm25 = BM25Okapi(tokenized)
    bm25_meta = {
        "chunk_ids": [r["chunk_id"] for r in all_chunks],
        "doc_ids": [r["doc_id"] for r in all_chunks],
        "page_nos": [r["page_no"] for r in all_chunks],
        "codes": [r["code"] for r in all_chunks],
        "fiscal_years": [r["fiscal_year"] for r in all_chunks],
        "texts": [r["text"] for r in all_chunks],
    }
    with open(BM25_PATH, "wb") as f:
        pickle.dump({"bm25": bm25, "meta": bm25_meta}, f)
    log.info("BM25 已建 %d 条 → %s", len(all_chunks), BM25_PATH.name)

    conn.close()
    return {"total_pages": len(rows), "embedded": len(need),
            "chunks": len(all_chunks), "dense": str(DENSE_PATH)}


def load_bm25() -> tuple[BM25Okapi, dict]:
    with open(BM25_PATH, "rb") as f:
        data = pickle.load(f)
    return data["bm25"], data["meta"]
