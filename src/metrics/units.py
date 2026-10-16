"""单位解析：「单位：元/万元…」行 → 归一化倍数。"""
from __future__ import annotations

import re

from .defs import UNIT_FACTORS

# 注意「元/股」不能误判为「元」：先排除每股类单位
_UNIT_LINE = re.compile(r"单位[:：]?\s*(千元|万元|百万元|亿元|元)(?:[/／]股)?(?:\s|$|。|币)")
_UNIT_ONLY = re.compile(r"单位[:：]?\s*(千元|万元|百万元|亿元|元)\s*$")


def parse_unit_lines(lines: list[str]) -> list[tuple[int, str, str]]:
    """在行列表中找单位声明。返回 [(行号, 单位词, 原行)]。"""
    found = []
    for i, ln in enumerate(lines):
        m = _UNIT_LINE.search(ln)
        if m:
            found.append((i, m.group(1), ln.strip()))
    return found


def unit_factor(unit: str) -> float:
    return UNIT_FACTORS.get(unit, 1.0)


def pick_unit(table_rows: list[list[str]], page_lines: list[str]) -> tuple[float, str, str]:
    """逐表关联单位：表内声明 > 页面声明（表上方就近，简化取页面最后一个单位行之前的最近一个）。

    返回 (倍数, 单位词, 来源: table|page|assumed)。
    """
    # 1) 表内
    for row in table_rows:
        for cell in row:
            c = str(cell or "")
            m = _UNIT_LINE.search(c)
            if m:
                return unit_factor(m.group(1)), m.group(1), "table"
    # 2) 页面（取页面中最后一个货币单位声明）
    hits = parse_unit_lines(page_lines)
    if hits:
        _, unit, _ = hits[-1]
        return unit_factor(unit), unit, "page"
    # 3) 默认元
    return 1.0, "元", "assumed"
