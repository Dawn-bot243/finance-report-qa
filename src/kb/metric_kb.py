"""财务指标知识库：FAQ 快速通道。

命中条件：问题含指标名 + 解释类意图（「是什么意思/定义/怎么算/代表什么」等），
或问题就是在问指标本身（「ROE是什么」）。直接返回知识库条目，不走检索与生成。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

import yaml

from .. import paths
from ..metrics.defs import norm_row_name

KB_PATH = paths.PROJECT_ROOT / "data" / "metric_kb.yaml"

# 解释类意图
_EXPLAIN_PAT = re.compile(
    r"什么意思|什么含义|含义|定义|解释|指什么|代表什么|意味着|怎么算|如何计算|怎么理解|如何理解|是什么"
)
# 明确的取值意图（与解释冲突时按取值处理）
_VALUE_PAT = re.compile(r"是多少|为多少|多少元|多少亿|多少万|数值|金额|余额为|达到多少")


@dataclass
class KBEntry:
    key: str
    name: str
    aliases: list[str]
    category: str
    definition: str
    formula: str
    interpretation: str
    caveats: str


@lru_cache(maxsize=1)
def load_kb() -> dict[str, KBEntry]:
    with open(KB_PATH, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    entries = {}
    for m in raw.get("metrics", []):
        entries[m["key"]] = KBEntry(
            key=m["key"], name=m["name"], aliases=m.get("aliases", []),
            category=m.get("category", ""), definition=m.get("definition", ""),
            formula=m.get("formula", ""), interpretation=m.get("interpretation", ""),
            caveats=m.get("caveats", ""),
        )
    return entries


@lru_cache(maxsize=1)
def _alias_index() -> list[tuple[str, str]]:
    """[(归一化别名, metric_key)]，按别名长度降序（长别名优先，避免「净利润」抢占「扣非净利润」）。"""
    pairs = []
    for key, e in load_kb().items():
        for a in [e.name, *e.aliases]:
            n = norm_row_name(a)
            if n:
                pairs.append((n, key))
    pairs.sort(key=lambda x: -len(x[0]))
    return pairs


def match_metric(question: str) -> KBEntry | None:
    """在问题中找知识库指标（最长别名优先）。"""
    q = norm_row_name(question)
    for alias, key in _alias_index():
        if alias in q:
            return load_kb()[key]
    return None


def is_faq(question: str, has_company_year: bool = False) -> bool:
    """是否为指标解释类问题。带公司+年份且只是要数值的，不走 FAQ。"""
    if not match_metric(question):
        return False
    if _VALUE_PAT.search(question) and not re.search(
            r"什么意思|什么含义|含义|定义|解释|指什么|代表什么|意味着|怎么算|如何计算|怎么理解|如何理解",
            question):
        return False
    if _EXPLAIN_PAT.search(question):
        return True
    # 无公司/年份的裸指标问句（如「ROE」）也按解释处理
    return not has_company_year and len(norm_row_name(question)) <= 12


def format_entry(e: KBEntry) -> str:
    return "\n".join([
        f"【指标解释】{e.name}（{e.category}）",
        f"定义：{e.definition}",
        f"计算：{e.formula}",
        f"如何解读：{e.interpretation}",
        f"注意事项：{e.caveats}",
    ])


def faq_answer(question: str, has_company_year: bool = False) -> str | None:
    if not is_faq(question, has_company_year):
        return None
    e = match_metric(question)
    return format_entry(e) if e else None
