"""生成：DeepSeek 仅依据检索上下文作答 + 行内引用 + 数值核验。"""
from __future__ import annotations

import re

from ..logutil import setup_logging
from .clients import chat
from .retriever import RetrievedChunk

log = setup_logging(__name__)

SYSTEM_PROMPT = """你是上市公司财报问答助手。规则：
1. 只能依据下方【检索到的财报原文】回答，不得使用外部知识。
2. 答案中每个关键陈述后必须标注来源，格式：【第N页】（N 为原文片段标注的页码）。
3. 涉及数字时，必须与原文数字完全一致，不得四舍五入或推算。
4. 原文没有答案时，直接回答「年报中未披露该信息」，不要编造。
5. 用简体中文简洁回答。"""

_NUM_TOKEN = re.compile(r"[-−－]?[\d,，]{2,}(?:\.\d+)?%?")


def _answer_numbers(text: str) -> list[str]:
    out = []
    for t in _NUM_TOKEN.findall(text):
        t = t.strip("，,、 ")
        if not t or re.match(r"^(?:19|20)\d\d$", t):
            continue
        out.append(t.replace(",", "").replace("，", "").lstrip("－−"))
    return out


def _verify_numbers(answer: str, chunks: list[RetrievedChunk]) -> list[str]:
    """数值核验：答案中每个数字须出现在任一检索片段（归一化数字串）。"""
    doc_digits = re.sub(r"[^\d.％%\-]", "", "".join(c.text for c in chunks))
    return [n for n in _answer_numbers(answer) if n not in doc_digits]


def build_context(chunks: list[RetrievedChunk], max_chars: int = 6000) -> str:
    parts = []
    total = 0
    for c in chunks:
        snippet = c.text[:1000]
        parts.append(f"【第{c.page_no}页】{c.code} FY{c.fiscal_year}\n{snippet}")
        total += len(snippet)
        if total >= max_chars:
            break
    return "\n\n".join(parts)


def generate(question: str, chunks: list[RetrievedChunk]) -> dict:
    """生成带引用回答。返回 {answer, citations, unchecked_numbers, refused}。"""
    if not chunks:
        return {"answer": "证据不足，无法回答（检索未命中任何相关段落）。",
                "citations": [], "unchecked_numbers": [], "refused": True}
    context = build_context(chunks)
    user = f"【检索到的财报原文】\n{context}\n\n【问题】\n{question}"
    answer = chat([{"role": "system", "content": SYSTEM_PROMPT},
                   {"role": "user", "content": user}])
    citations = [f"第{c.page_no}页（{c.code} FY{c.fiscal_year}）" for c in chunks[:5]]
    unchecked = _verify_numbers(answer, chunks)
    if unchecked:
        answer += "\n\n⚠️ 数值存疑：以下数字未能从检索原文核验：" + "、".join(unchecked)
    return {"answer": answer, "citations": citations,
            "unchecked_numbers": unchecked, "refused": False}
