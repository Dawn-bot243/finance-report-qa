"""混合检索：查询分析（公司/年份/指标）→ numpy 稠密向量 + BM25 → RRF 融合 → 重排。"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

import numpy as np

from .. import paths
from ..config import Config, load
from ..logutil import setup_logging
from ..metrics.defs import METRIC_DEFS, norm_row_name
from .clients import rerank
from .indexer import DENSE_PATH, load_bm25

log = setup_logging(__name__)

RRF_K = 60

# 模块级缓存：BM25（pickle 加载较慢）与稠密向量进程内只建一次
_BM25_CACHE: dict = {}
_DENSE_CACHE: dict = {}


def _bm25():
    if "data" not in _BM25_CACHE:
        _BM25_CACHE["data"] = load_bm25()
    return _BM25_CACHE["data"]


def _dense():
    if "data" not in _DENSE_CACHE:
        _DENSE_CACHE["data"] = dict(np.load(DENSE_PATH, allow_pickle=True))
    return _DENSE_CACHE["data"]


@dataclass
class RetrievedChunk:
    chunk_id: int
    doc_id: int
    page_no: int
    code: str
    fiscal_year: int
    text: str
    score: float = 0.0
    origin: str = ""  # chroma|bm25|rrf

    @property
    def key(self) -> tuple:
        return (self.chunk_id,)


@dataclass
class QueryInfo:
    code: str | None = None
    company_name: str | None = None
    year: int | None = None
    metric_keys: list[str] = field(default_factory=list)

    @property
    def is_structured(self) -> bool:
        return bool(self.code and self.year and self.metric_keys)


def analyze_query(question: str, cfg: Config) -> QueryInfo:
    """正则提取公司/年份/指标（不调 LLM，快且确定）。"""
    info = QueryInfo()
    for c in cfg.companies:
        if c.name in question or c.code in question:
            info.code, info.company_name = c.code, c.name
            break
    m = re.search(r"(20\d\d)", question)
    if m and int(m.group(1)) in cfg.fiscal_years:
        info.year = int(m.group(1))
    q_norm = norm_row_name(question)
    # 别名子串碰撞守卫：「合同资产总额」⊃「资产总额」、「未分配利润总额」⊃「利润总额」
    _BLOCK = {"total_assets": ("合同资产", "净资产总额"),
              "total_profit": ("未分配利润",),
              "net_profit": ("未分配利润",)}
    for mdef in METRIC_DEFS:
        if mdef.yoy_of:
            continue
        blocked = any(b in question for b in _BLOCK.get(mdef.key, ()))
        if blocked:
            continue
        for a in mdef.aliases:
            if norm_row_name(a) in q_norm and mdef.key not in info.metric_keys:
                info.metric_keys.append(mdef.key)
    return info


# 问题模板噪音词（公司名/年份由元数据过滤承担，正文检索时剔除，实测 hit@10 从 4/10 提到 9/10）
_TEMPLATE_NOISE = re.compile(
    r"^(?:19|20)\d\d年?|有哪些|是多少|多少|情况|如何|什么|是否|请问|请|"
    r"的|在|了|是|吗|呢|？|\?|。|、|，|,|\s+"
)


def clean_query(question: str, info: QueryInfo) -> str:
    """检索用查询清洗：去公司名/年份/模板词，保留核心意图词。"""
    t = question
    if info.company_name:
        t = t.replace(info.company_name, "")
    if info.code:
        t = t.replace(info.code, "")
    t = _TEMPLATE_NOISE.sub(" ", t)
    return t.strip() or question


def _dense_search(query: str, code: str | None, year: int | None, n: int = 20) -> list[RetrievedChunk]:
    """numpy 余弦检索：查询向量 × 全库矩阵，元数据掩码过滤，取 top-n。"""
    dense = _dense()
    vectors: np.ndarray = dense["vectors"]
    codes: np.ndarray = dense["codes"]
    years: np.ndarray = dense["fiscal_years"]
    from .clients import embed
    qv = np.array(embed([query])[0], dtype=np.float32)
    qv = qv / (np.linalg.norm(qv) + 1e-9)
    norms = np.linalg.norm(vectors, axis=1) + 1e-9
    sims = vectors @ qv / norms
    mask = np.ones(len(sims), dtype=bool)
    if code:
        mask &= codes == code
    if year:
        mask &= years == year
    idx = np.argsort(-sims)[: n * 5]
    out = []
    for i in idx:
        if not mask[i]:
            continue
        out.append(RetrievedChunk(
            chunk_id=int(dense["chunk_ids"][i]), doc_id=int(dense["doc_ids"][i]),
            page_no=int(dense["page_nos"][i]), code=str(codes[i]),
            fiscal_year=int(years[i]), text=str(dense["texts"][i]),
            score=float(sims[i]), origin="dense",
        ))
        if len(out) >= n:
            break
    return out


def _bm25_search(query: str, code: str | None, year: int | None, n: int = 20) -> list[RetrievedChunk]:
    bm25, meta = _bm25()
    from .indexer import _tokenize
    scores = bm25.get_scores(_tokenize(query))
    idx = sorted(range(len(scores)), key=lambda i: -scores[i])
    out = []
    for i in idx:
        if code and meta["codes"][i] != code:
            continue
        if year and meta["fiscal_years"][i] != year:
            continue
        if scores[i] <= 0:
            continue
        out.append(RetrievedChunk(
            chunk_id=meta["chunk_ids"][i], doc_id=meta["doc_ids"][i], page_no=meta["page_nos"][i],
            code=meta["codes"][i], fiscal_year=meta["fiscal_years"][i],
            text=meta["texts"][i], score=float(scores[i]), origin="bm25",
        ))
        if len(out) >= n:
            break
    return out


def _rrf_fuse(lists: list[list[RetrievedChunk]], top_n: int = 10) -> list[RetrievedChunk]:
    merged: dict[tuple[int, int], RetrievedChunk] = {}
    for lst in lists:
        for rank, c in enumerate(lst):
            key = (c.doc_id, c.page_no)
            if key not in merged:
                merged[key] = c
                merged[key].score = 0.0
                merged[key].origin = "rrf"
            merged[key].score += 1.0 / (RRF_K + rank + 1)
    ranked = sorted(merged.values(), key=lambda c: -c.score)[:top_n]
    return ranked


def retrieve(question: str, cfg: Config | None = None,
             code: str | None = None, year: int | None = None,
             top_k: int = 5) -> list[RetrievedChunk]:
    """混合检索 → 重排 → top_k。"""
    cfg = cfg or load()
    info = analyze_query(question, cfg)
    code = code or info.code
    year = year or info.year
    cq = clean_query(question, info)

    dense_hits = _dense_search(cq, code, year, n=20)
    bm25_hits = _bm25_search(cq, code, year, n=20)
    fused = _rrf_fuse([dense_hits, bm25_hits], top_n=10)
    if not fused:
        return []
    # 重排（用清洗后的查询）
    docs = [c.text for c in fused]
    try:
        pairs = rerank(cq, docs, top_n=min(top_k, len(docs)))
        out = []
        for i, score in pairs:
            c = fused[i]
            c.score = score
            out.append(c)
        return out
    except Exception as e:
        log.warning("重排失败，退回 RRF 结果: %s", e)
        for c in fused[:top_k]:
            c.origin = "rrf-fallback"
        return fused[:top_k]


def chunk_citation(conn: sqlite3.Connection, c: RetrievedChunk) -> str:
    """引用行：文档标题 + 页码。"""
    r = conn.execute("SELECT title FROM documents WHERE doc_id=?", (c.doc_id,)).fetchone()
    title = r["title"] if r else "?"
    return f"【{title} 第{c.page_no}页】"
