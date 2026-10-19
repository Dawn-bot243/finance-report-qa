"""自校正检索（Corrective RAG）：检索质量评估 → 查询重写 → 重新检索。

流程：
    retrieve(q) → assess(q, chunks)
        ├─ 证据充分 → 返回
        └─ 不足 → rewrite(q, reason) → retrieve(q') → 再评估（最多 max_rewrites 轮）
    重写耗尽仍不充分 → 标记 insufficient，由上层拒答（不硬生成）

评估用混合策略（省调用且准）：
    1. rerank top-1 分数 >= FAST_PASS_SCORE → 直接放行，不调 LLM
    2. 否则 LLM 复核：问题 + top-3 片段摘要 → 能否回答
"""
from __future__ import annotations

import re

from ..config import Config
from ..logutil import setup_logging
from .clients import chat
from .retriever import RetrievedChunk, retrieve

log = setup_logging(__name__)

FAST_PASS_SCORE = 0.5   # top-1 rerank 分数高于此值视为充分，跳过 LLM 复核
MAX_SNIPPET = 500       # 评估时每个片段截取长度

_ASSESS_PROMPT = """你是财报检索质量评估员。判断【检索到的片段】是否包含回答【问题】所需的信息。

【问题】{question}

【检索到的片段】
{snippets}

只输出一个 JSON 对象（不要其他文字）：
{{"sufficient": true 或 false, "reason": "一句话说明"}}
判定标准：片段中明确出现了回答问题所需的数字/事实 → true；只是主题相关但缺少关键信息 → false。"""

_REWRITE_PROMPT = """你是财报检索查询改写器。原查询检索到的片段不足以回答问题，需要换一种检索表达。

【原问题】{question}
【失败原因】{reason}
【已检索片段的内容主题】
{snippets}

请输出一条更适合在 A 股年报全文中检索的查询：
- 保留核心实体词、指标名、业务/产品/项目名称
- 去掉「是多少」「有哪些」「情况如何」等模板词
- 可以补充同义词或更具体的表述
只输出改写后的查询文本，不要解释。"""


def _snippets(chunks: list[RetrievedChunk], n: int = 3) -> str:
    parts = []
    for i, c in enumerate(chunks[:n], 1):
        parts.append(f"[{i}] {c.text[:MAX_SNIPPET]}")
    return "\n\n".join(parts)


def _parse_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {}
    try:
        import json
        return json.loads(m.group(0))
    except Exception:
        return {}


def assess(question: str, chunks: list[RetrievedChunk]) -> dict:
    """判断检索结果能否回答问题。返回 {sufficient, reason, mode}。"""
    if not chunks:
        return {"sufficient": False, "reason": "检索未命中任何片段", "mode": "score"}

    top_score = float(chunks[0].score or 0.0)
    if top_score >= FAST_PASS_SCORE:
        return {"sufficient": True,
                "reason": f"rerank top-1={top_score:.2f} ≥ {FAST_PASS_SCORE}",
                "mode": "score"}

    try:
        out = chat([{"role": "user", "content": _ASSESS_PROMPT.format(
            question=question, snippets=_snippets(chunks))}],
            temperature=0, max_tokens=120)
        data = _parse_json(out)
        if "sufficient" in data:
            return {"sufficient": bool(data["sufficient"]),
                    "reason": str(data.get("reason", ""))[:200], "mode": "llm"}
    except Exception as e:
        log.warning("相关性评估失败，按充分处理: %s", e)
    # 评估失败时保守放行，避免把 API 故障误判成证据不足
    return {"sufficient": True, "reason": "评估调用失败，保守放行", "mode": "fallback"}


def rewrite(question: str, chunks: list[RetrievedChunk], reason: str) -> str:
    """LLM 重写查询；失败时退回原问题。"""
    try:
        out = chat([{"role": "user", "content": _REWRITE_PROMPT.format(
            question=question, reason=reason, snippets=_snippets(chunks, 2))}],
            temperature=0.2, max_tokens=100)
        q = out.strip().strip('"').strip()
        # 过滤明显异常的输出（过长/多行/空）
        if q and len(q) <= 80 and "\n" not in q:
            return q
    except Exception as e:
        log.warning("查询重写失败: %s", e)
    return question


def retrieve_corrective(question: str, cfg: Config, top_k: int = 5,
                        max_rewrites: int = 2) -> dict:
    """自校正检索循环。

    返回 {chunks, query, rounds, rewrites:[{from,to,reason}], sufficient, assessment}
    """
    query = question
    rewrite_log: list[dict] = []
    chunks: list[RetrievedChunk] = []
    verdict = {"sufficient": False, "reason": "", "mode": ""}

    for rnd in range(max_rewrites + 1):
        chunks = retrieve(query, cfg, top_k=top_k)
        verdict = assess(question, chunks)
        if verdict["sufficient"]:
            return {"chunks": chunks, "query": query, "rounds": rnd + 1,
                    "rewrites": rewrite_log, "sufficient": True, "assessment": verdict}
        if rnd == max_rewrites:
            break
        new_query = rewrite(question, chunks, verdict["reason"])
        if new_query == query:  # 重写无变化，避免无效循环
            break
        log.info("查询重写 #%d: %s → %s（%s）", rnd + 1, query, new_query, verdict["reason"])
        rewrite_log.append({"from": query, "to": new_query, "reason": verdict["reason"]})
        query = new_query

    return {"chunks": chunks, "query": query, "rounds": len(rewrite_log) + 1,
            "rewrites": rewrite_log, "sufficient": False, "assessment": verdict}
