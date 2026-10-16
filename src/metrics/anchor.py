"""锚定取值：行名归一化匹配、数值解析白盒规则、正则兜底。"""
from __future__ import annotations

import re

from .defs import MetricDef, norm_row_name

_NUM_RE = re.compile(r"[-−－]?\d[\d,，]*\.?\d*")
_NEG_MARK = re.compile(r"[（(]\s*[-−－]?\d[\d,，]*\.?\d*\s*[)）]")
_NIL_WORDS = ("不适用", "无", "—", "-", "/", "N/A", "n/a", "", "未披露")


def parse_number(cell: str) -> float | None:
    """白盒数值解析：千分位/全角/括号负数/负号变体/「不适用」。"""
    s = str(cell or "").strip()
    if s in _NIL_WORDS:
        return None
    # 括号负数：(1,234.5) → -1234.5
    m = _NEG_MARK.search(s)
    if m:
        inner = m.group(0)[1:-1]
        n = _NUM_RE.search(inner)
        if n:
            return -float(n.group(0).replace(",", "").replace("，", ""))
    # 负号变体
    m = re.search(r"[-−－]\s*\d[\d,，]*\.?\d*", s)
    if m:
        return -float(m.group(0)[1:].replace(",", "").replace("，", ""))
    m = _NUM_RE.search(s)
    if not m:
        return None
    val = float(m.group(0).replace(",", "").replace("，", ""))
    # 「减少/下降」前缀 → 负数（增减幅度列常见：「减少1.23个百分点」）
    if re.search(r"减少|下降|降低", s):
        val = -val
    return val


def anchor_rows(rows: list[list[str]], mdef: MetricDef, kind: str) -> list[int]:
    """找目标指标的行号：行名（第0列）归一化后与别名匹配。
    精确优先；否则允许别名出现在行名中且长度差 ≤6（容忍「其中：」等前缀残留）。"""
    hits = []
    aliases = [norm_row_name(a) for a in mdef.aliases]
    for i, row in enumerate(rows):
        if not row:
            continue
        label = norm_row_name(str(row[0] or ""))
        if not label:
            continue
        if label in aliases:
            hits.append(i)
            continue
        for a in aliases:
            if a and a in label and len(label) - len(a) <= 6:
                hits.append(i)
                break
    return hits


def _alias_re(mdef: MetricDef) -> str:
    return "|".join(re.escape(a) for a in sorted(mdef.aliases, key=len, reverse=True))


def regex_fallback(page_text: str, mdefs: list[MetricDef], kind: str) -> list[dict]:
    """正则兜底：行内「别名[：:]?数值」。返回 [{metric_key, value, raw_value, line}]。"""
    out = []
    for mdef in mdefs:
        if kind not in mdef.tables:
            continue
        pat = re.compile(rf"({_alias_re(mdef)})\s*[：:]?\s*([-−－]?\d[\d,，]*\.?\d*%?)")
        for m in pat.finditer(page_text):
            val = parse_number(m.group(2))
            if val is None:
                continue
            out.append({
                "metric_key": mdef.key,
                "value": val,
                "raw_value": m.group(2),
                "line": m.group(0)[:80],
            })
    return out
