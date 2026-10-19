"""FastAPI 后端：问答 / 指标 / 同业对比 / 指标解释。

启动：
    python -X utf8 run_all.py serve --port 8000
    或 uvicorn src.api.server:app --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import asdict

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .. import paths
from ..analysis.compare import CORE_METRICS, Cell, _best_row, _display, compare_peers, render_markdown
from ..config import load
from ..kb.metric_kb import format_entry, match_metric
from ..logutil import setup_logging
from ..metrics.defs import METRIC_BY_KEY
from ..rag.ask import ask
from .security import security_middleware

log = setup_logging(__name__)

app = FastAPI(title="财报智能问答 API", version="0.5.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)
app.middleware("http")(security_middleware)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(paths.db_path())
    conn.row_factory = sqlite3.Row
    return conn


class AskRequest(BaseModel):
    question: str
    top_k: int = 5


class AgentRequest(BaseModel):
    question: str
    max_iters: int = 6
    corrective: bool = True


@app.get("/health")
def health() -> dict:
    try:
        with closing(_conn()) as conn:
            docs = conn.execute("SELECT COUNT(*) c FROM documents").fetchone()["c"]
            pages = conn.execute("SELECT COUNT(*) c FROM pages").fetchone()["c"]
            metrics = conn.execute("SELECT COUNT(*) c FROM metrics").fetchone()["c"]
        return {"status": "ok", "documents": docs, "pages": pages, "metrics": metrics}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"数据库不可用: {e}")


@app.get("/companies")
def companies() -> list[dict]:
    cfg = load()
    with closing(_conn()) as conn:
        rows = conn.execute("SELECT code, name FROM companies ORDER BY code").fetchall()
    known = {c.code: c for c in cfg.companies}
    return [{"code": r["code"], "name": r["name"], "org_id": known.get(r["code"]).org_id if known.get(r["code"]) else None}
            for r in rows]


@app.post("/ask")
def ask_endpoint(req: AskRequest) -> dict:
    """问答：FAQ → 结构化直答 → 混合检索生成（含引用与数值核验）。"""
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="question 不能为空")
    try:
        return ask(req.question, load(), top_k=req.top_k)
    except Exception as e:
        log.exception("ask 失败")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/agent")
def agent_endpoint(req: AgentRequest) -> dict:
    """工具调用型 Agent：LLM 自主决定调用哪个工具，返回答案 + 工具轨迹。"""
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="question 不能为空")
    if not 1 <= req.max_iters <= 10:
        raise HTTPException(status_code=400, detail="max_iters 需在 1-10 之间")
    try:
        import time
        from ..agent.loop import agent_ask
        t0 = time.time()
        res = agent_ask(req.question.strip(), load(), max_iters=req.max_iters,
                        corrective=req.corrective)
        return {**res, "elapsed_s": round(time.time() - t0, 1)}
    except Exception as e:
        log.exception("agent 失败")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/metric_defs")
def metric_defs() -> list[dict]:
    """全部指标定义（供前端选择指标）。"""
    return [{"key": k, "name": v.cn_name, "category": v.category, "unit_type": v.unit_type}
            for k, v in METRIC_BY_KEY.items()]


@app.get("/kb")
def kb_endpoint(question: str = Query(..., description="指标解释类问题")) -> dict:
    entry = match_metric(question)
    if entry is None:
        return {"matched": False}
    return {"matched": True, "key": entry.key, "entry": asdict(entry),
            "answer": format_entry(entry)}


@app.get("/metrics")
def metrics_endpoint(code: str, year: int,
                     keys: str | None = Query(None, description="逗号分隔指标 key，默认核心15项")) -> dict:
    """单公司单财年结构化指标（含出处）。"""
    metric_keys = [k.strip() for k in keys.split(",")] if keys else CORE_METRICS
    metric_keys = [k for k in metric_keys if k in METRIC_BY_KEY]
    with closing(_conn()) as conn:
        company = conn.execute("SELECT name FROM companies WHERE code=?", (code,)).fetchone()
        if not company:
            raise HTTPException(status_code=404, detail=f"未知公司代码 {code}")
        out = []
        for key in metric_keys:
            row = _best_row(conn, code, year, key)
            if row is None or row["value"] is None:
                out.append({"key": key, "name": METRIC_BY_KEY[key].cn_name,
                            "status": "missing", "display": "未披露"})
            else:
                out.append({
                    "key": key, "name": METRIC_BY_KEY[key].cn_name,
                    "value": float(row["value"]),
                    "display": _display(float(row["value"]), key),
                    "unit_type": METRIC_BY_KEY[key].unit_type,
                    "basis": row["basis"], "source": row["source"],
                    "doc_title": row["title"], "doc_type": row["doc_type"],
                    "page_no": row["page_no"], "status": "ok",
                })
    return {"code": code, "company": company["name"], "year": year, "metrics": out}


@app.get("/compare")
def compare_endpoint(codes: str, year: int,
                     metrics: str | None = Query(None, description="逗号分隔指标 key")) -> dict:
    """同业对比：2-3 家公司 + 同一报告期。"""
    code_list = [c.strip() for c in codes.split(",") if c.strip()]
    if not 2 <= len(code_list) <= 3:
        raise HTTPException(status_code=400, detail="请选择 2-3 家公司")
    metric_keys = [m.strip() for m in metrics.split(",")] if metrics else None
    with closing(_conn()) as conn:
        missing = [c for c in code_list
                   if conn.execute("SELECT 1 FROM companies WHERE code=?", (c,)).fetchone() is None]
        if missing:
            raise HTTPException(status_code=404, detail=f"未知公司代码: {missing}")
        res = compare_peers(conn, code_list, year, metric_keys)
    return {
        "year": res["year"],
        "companies": [{"code": c, "name": n} for c, n in res["companies"]],
        "comparable_rows": res["comparable_rows"],
        "total_rows": res["total_rows"],
        "caveats": res["caveats"],
        "markdown": render_markdown(res),
        "rows": [
            {"key": r["key"], "name": r["name"],
             "cells": [asdict(c) for c in r["cells"]],
             "caveats": r["caveats"]}
            for r in res["rows"]
        ],
    }
