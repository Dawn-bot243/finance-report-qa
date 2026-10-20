"""语义切分：把一页年报文本切成若干子块（父子块索引的「子块」）。

规则：
- 以标题行为边界（第X节 / 一、 / （一） / 数字编号）
- 块超过 max_chars 时在非表格行处强制切分
- 连续表格行尽量保持完整（表格内不切），但单块超过 2×max_chars 时按行切
- 过短的尾块并入前一块

子块用于检索（更聚焦），父块仍是整页（用于生成上下文与【第N页】引用）。
"""
from __future__ import annotations

import hashlib
import re

MAX_CHARS = 500
MIN_CHARS = 120
TABLE_ROW = " | "

_HEADING = re.compile(
    r"^(?:第[一二三四五六七八九十百]+节"
    r"|[一二三四五六七八九十]+、"
    r"|[（(][一二三四五六七八九十]+[）)]"
    r"|\d{1,2}[、.．]\s*\S)"
)


def _is_heading(line: str) -> bool:
    s = line.strip()
    return bool(_HEADING.match(s)) and len(s) <= 80


def split_text(text: str, max_chars: int = MAX_CHARS,
               min_chars: int = MIN_CHARS) -> list[str]:
    """把一页文本切成子块。返回文本列表（至少 1 块）。"""
    lines = [ln for ln in text.split("\n") if ln.strip()]
    if not lines:
        return []

    chunks: list[str] = []
    cur: list[str] = []
    cur_len = 0

    def flush() -> None:
        nonlocal cur, cur_len
        if cur:
            chunks.append("\n".join(cur))
            cur, cur_len = [], 0

    for ln in lines:
        heading = _is_heading(ln)
        is_table = TABLE_ROW in ln

        # 标题行且当前块已够长 → 先切
        if heading and cur_len >= min_chars:
            flush()

        cur.append(ln)
        cur_len += len(ln)

        # 超长：非表格行处切；表格行只在极端情况下切，避免破坏表格
        if cur_len >= max_chars and (not is_table or cur_len >= max_chars * 2):
            flush()

    flush()

    # 合并过短块（< min_chars）到前一块
    merged: list[str] = []
    for c in chunks:
        if merged and len(c) < min_chars and len(merged[-1]) + len(c) <= max_chars * 1.6:
            merged[-1] = merged[-1] + "\n" + c
        else:
            merged.append(c)
    return merged or [text]


def split_page(text: str, page_no: int) -> list[dict]:
    """切分一页，返回 [{page_no, seq, text, sha256}]。"""
    out = []
    for seq, c in enumerate(split_text(text)):
        out.append({"page_no": page_no, "seq": seq, "text": c,
                    "sha256": hashlib.sha256(c.encode("utf-8")).hexdigest()})
    return out
