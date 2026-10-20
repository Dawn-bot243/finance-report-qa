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
from .clients import chat, rerank
from .indexer import DENSE_PATH, load_bm25

log = setup_logging(__name__)

RRF_K = 60
MQ_WEIGHT = 0.5  # 多查询时改写查询的 RRF 权重（原查询为 1.0）

# 模块级缓存：BM25（pickle 加载较慢）与稠密向量进程内只建一次
_BM25_CACHE: dict = {}
_DENSE_CACHE: dict = {}


def _bm25():
    if "data" not in _BM25_CACHE:
        _BM25_CACHE["data"] = load_bm25()
    return _BM25_CACHE["data"]


def _dense():
    if "data" not in _DENSE_CACHE:
        d = dict(np.load(DENSE_PATH, allow_pickle=True))
        if str(d.get("version", "")) != "child_v1":
            raise RuntimeError("稠密索引是旧版页级格式，请先运行 "
                               "python -X utf8 run_all.py rag build --force")
        _DENSE_CACHE["data"] = d
    return _DENSE_CACHE["data"]


@dataclass
class RetrievedChunk:
    chunk_id: int
    doc_id: int
    page_no: int
    code: str
    fiscal_year: int
    text: str          # 父页全文（供生成与引用）
    score: float = 0.0
    origin: str = ""   # dense|bm25|rrf
    child_text: str = ""  # 命中的语义子块原文（检索粒度）

    @property
    def key(self) -> tuple:
        return (self.doc_id, self.page_no)


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


def _dense_page_hits(query: str, code: str | None, year: int | None,
                     n: int = 20) -> list[RetrievedChunk]:
    """子块级稠密检索 → 聚合到页（同页取最高分子块）。

    返回页级候选，text 暂为命中的子块文本，由 retrieve() 回溯为父页全文。
    """
    dense = _dense()
    if "seqs" not in dense:
        raise RuntimeError("稠密索引是旧版页级格式，请先运行 rag build --force 重建")
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
    idx = np.argsort(-sims)[: n * 8]  # 子块候选放宽，聚合后取页级 top-n
    agg: dict[tuple[int, int], tuple[float, int]] = {}
    for i in idx:
        if not mask[i]:
            continue
        key = (int(dense["doc_ids"][i]), int(dense["page_nos"][i]))
        s = float(sims[i])
        if key not in agg or s > agg[key][0]:
            agg[key] = (s, int(i))
    out = []
    for (did, pg), (s, i) in sorted(agg.items(), key=lambda x: -x[1][0])[:n]:
        out.append(RetrievedChunk(
            chunk_id=did * 100000 + pg * 100 + int(dense["seqs"][i]),
            doc_id=did, page_no=pg,
            code=str(codes[i]), fiscal_year=int(years[i]),
            text=str(dense["texts"][i]), score=s, origin="dense",
            child_text=str(dense["texts"][i]),
        ))
    return out


def _bm25_page_hits(query: str, code: str | None, year: int | None,
                    n: int = 20) -> list[RetrievedChunk]:
    """子块级 BM25 检索 → 聚合到页（同页取最高分子块）。"""
    bm25, meta = _bm25()
    from .indexer import _tokenize
    scores = bm25.get_scores(_tokenize(query))
    order = np.argsort(-scores)
    agg: dict[tuple[int, int], tuple[float, int]] = {}
    for i in order:
        i = int(i)
        if scores[i] <= 0:
            break
        if code and meta["codes"][i] != code:
            continue
        if year and meta["fiscal_years"][i] != year:
            continue
        key = (meta["doc_ids"][i], meta["page_nos"][i])
        s = float(scores[i])
        if key not in agg or s > agg[key][0]:
            agg[key] = (s, i)
        if len(agg) >= n * 3:
            break
    out = []
    for (did, pg), (s, i) in sorted(agg.items(), key=lambda x: -x[1][0])[:n]:
        out.append(RetrievedChunk(
            chunk_id=meta["child_ids"][i], doc_id=did, page_no=pg,
            code=meta["codes"][i], fiscal_year=meta["fiscal_years"][i],
            text=meta["texts"][i], score=s, origin="bm25",
            child_text=meta["texts"][i],
        ))
    return out


_MQ_PROMPT = """把下面的财报问题改写成 {n} 个不同角度的检索查询，用于在年报全文中查找证据。

【原问题】{question}

要求：
- 保留公司名、年份等实体；换用年报里可能的表述（如「营收」→「营业收入」「主营业务收入」）
- 每个查询聚焦一个方面（指标名、业务板块、时间范围、同义表述）
- 每行一个查询，不要编号，不要解释，最多 {n} 行"""


def generate_queries(question: str, n: int = 3) -> list[str]:
    """LLM 生成多角度检索查询（Multi-Query）。失败返回空列表，不影响主流程。"""
    try:
        out = chat([{"role": "user", "content": _MQ_PROMPT.format(question=question, n=n)}],
                   temperature=0.3, max_tokens=160)
        qs = [ln.strip().lstrip("-•0123456789.、 ").strip()
              for ln in out.splitlines() if ln.strip()]
        qs = [q for q in qs if 4 <= len(q) <= 100]
        return qs[:n]
    except Exception as e:
        log.warning("多查询生成失败: %s", e)
        return []


def _parent_texts(pairs: list[tuple[int, int]]) -> dict[tuple[int, int], str]:
    """批量取父页全文（命中子块回溯用）。"""
    if not pairs:
        return {}
    conn = sqlite3.connect(paths.db_path())
    try:
        out = {}
        for did, pg in pairs:
            r = conn.execute("SELECT text FROM pages WHERE doc_id=? AND page_no=?",
                             (did, pg)).fetchone()
            if r:
                out[(did, pg)] = r[0]
        return out
    finally:
        conn.close()


def _rrf_fuse(lists: list[list[RetrievedChunk]], top_n: int = 10,
              weights: list[float] | None = None) -> list[RetrievedChunk]:
    """RRF 融合。weights 可按列表加权——多查询时原查询权重更高，
    避免改写查询的噪音稀释主查询的排名信号。"""
    merged: dict[tuple[int, int], RetrievedChunk] = {}
    for li, lst in enumerate(lists):
        w = weights[li] if weights and li < len(weights) else 1.0
        for rank, c in enumerate(lst):
            key = (c.doc_id, c.page_no)
            if key not in merged:
                merged[key] = c
                merged[key].score = 0.0
                merged[key].origin = "rrf"
            merged[key].score += w / (RRF_K + rank + 1)
    ranked = sorted(merged.values(), key=lambda c: -c.score)[:top_n]
    return ranked


def retrieve(question: str, cfg: Config | None = None,
             code: str | None = None, year: int | None = None,
             top_k: int = 5, queries: list[str] | None = None) -> list[RetrievedChunk]:
    """混合检索 → 重排 → top_k（子块检索、父页回溯）。

    queries：额外的查询改写（多查询检索）；与原查询一起做 RRF 融合。
    """
    cfg = cfg or load()
    info = analyze_query(question, cfg)
    code = code or info.code
    year = year or info.year
    qlist = [clean_query(question, info)]
    for q in (queries or []):
        qc = clean_query(q, info)
        if qc and qc not in qlist:
            qlist.append(qc)

    hit_lists, weights = [], []
    for qi, q in enumerate(qlist):
        w = 1.0 if qi == 0 else MQ_WEIGHT  # 原查询权重高于改写查询
        hit_lists.append(_dense_page_hits(q, code, year, n=20))
        weights.append(w)
        hit_lists.append(_bm25_page_hits(q, code, year, n=20))
        weights.append(w)
    fused = _rrf_fuse(hit_lists, top_n=10, weights=weights)
    if not fused:
        return []

    # 重排用父页文本：子块缺少表头/上下文（如「营业收入」与数值分离），
    # 纯子块重排会误判相关性；父页保留完整语义，截断到 1200 字控制成本。
    parent = _parent_texts([(c.doc_id, c.page_no) for c in fused])
    docs = [(parent.get((c.doc_id, c.page_no)) or c.child_text or c.text)[:1200]
            for c in fused]
    try:
        pairs = rerank(qlist[0], docs, top_n=min(top_k, len(docs)))
        out = []
        for i, score in pairs:
            c = fused[i]
            c.score = score
            out.append(c)
    except Exception as e:
        log.warning("重排失败，退回 RRF 结果: %s", e)
        out = fused[:top_k]
        for c in out:
            c.origin = "rrf-fallback"

    # 父页回溯：text 换成整页全文，供生成与引用（复用 rerank 前的 parent 缓存）
    for c in out:
        c.text = parent.get((c.doc_id, c.page_no), c.child_text or c.text)
    return out


def chunk_citation(conn: sqlite3.Connection, c: RetrievedChunk) -> str:
    """引用行：文档标题 + 页码。"""
    r = conn.execute("SELECT title FROM documents WHERE doc_id=?", (c.doc_id,)).fetchone()
    title = r["title"] if r else "?"
    return f"【{title} 第{c.page_no}页】"
