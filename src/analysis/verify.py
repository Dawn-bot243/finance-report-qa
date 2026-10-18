"""数值核验：LLM 生成文本中的「指标=数值」与结构化指标库原值比对。

只核验可归因的数字：文本中「指标别名 + （为/是/达到/：）+ 数字」模式。
这样避免把叙述性数字（职工薪酬、页内其它数值）误判为指标值。

判定：
- match：与库中该指标值相对误差 ≤0.5%（支持万/亿/千单位换算）
- suspicious：超出容差（>0.5%）→ 标记待核实
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from ..metrics.defs import METRIC_BY_KEY, METRIC_DEFS

_CITE = re.compile(r"【[^】]*】")
_TOKEN = r"[-−－]?\d[\d,，]*(?:\.\d+)?[万亿千百]?%?"
_YEAR = re.compile(r"^(?:19|20)\d\d$")
_UNIT_MULT = {"万": 1e4, "亿": 1e8, "千": 1e3, "百": 1e2}
# 别名与数值之间允许的桥接/连接词（全部可选；不含「占…比例」等非取值结构）
_CONNECT = (r"\s*(?:合计|总额|本期数|本期金额|期末余额|本期|当年|报告期|金额|余额)?"
            r"\s*(?:为|是|达到|达|约|：|:|＝|=)?\s*")


@dataclass
class Finding:
    token: str
    value: float
    status: str          # match | suspicious
    metric_key: str
    metric_name: str
    db_value: float
    db_source: str
    rel_err: float


def _parse(raw: str) -> tuple[float, bool] | None:
    m = re.match(r"^([-−－]?\d[\d,，]*(?:\.\d+)?)([万亿千百]?)(%?)$", raw)
    if not m:
        return None
    num, unit, pct = m.groups()
    clean = num.replace(",", "").replace("，", "").lstrip("−－")
    if not clean or _YEAR.match(clean):
        return None
    # 单个数字且无单位/百分号/小数 → 计数类，跳过
    if len(num) == 1 and not unit and not pct:
        return None
    try:
        return float(clean) * _UNIT_MULT.get(unit, 1.0), bool(pct)
    except ValueError:
        return None


def _alias_patterns() -> list[tuple[str, str]]:
    """[(alias, metric_key)] 按别名长度降序。"""
    pairs = []
    for mdef in METRIC_DEFS:
        for a in mdef.aliases:
            pairs.append((a, mdef.key))
    pairs.sort(key=lambda x: -len(x[0]))
    return pairs


def _db_rows(conn: sqlite3.Connection, key: str, code: str | None, year: int | None):
    q = ("SELECT value, source, code, fiscal_year, basis FROM metrics "
         "WHERE metric_key=? AND value IS NOT NULL")
    args: list = [key]
    if code:
        q += " AND code=?"
        args.append(code)
    if year:
        q += " AND fiscal_year=?"
        args.append(year)
    q += (" ORDER BY CASE basis WHEN 'as_reported' THEN 0 ELSE 1 END,"
          " CASE WHEN doc_id IS NOT NULL THEN 0 ELSE 1 END LIMIT 5")
    return conn.execute(q, args).fetchall()


def verify_numbers(conn: sqlite3.Connection, text: str,
                   code: str | None = None, year: int | None = None,
                   tolerance: float = 0.005) -> dict:
    """核验文本中直接陈述的指标值。返回 {matched, suspicious: [Finding], total}。"""
    t = _CITE.sub(" ", text)
    findings: list[Finding] = []
    matched = 0
    used_spans: list[tuple[int, int]] = []

    for alias, key in _alias_patterns():
        pat = re.compile(re.escape(alias) + _CONNECT + f"({_TOKEN})")
        for m in pat.finditer(t):
            span = m.span()
            if any(not (span[1] <= s or span[0] >= e) for s, e in used_spans):
                continue
            parsed = _parse(m.group(1))
            if parsed is None:
                continue
            value, is_pct = parsed
            if value == 0:
                continue  # 零值无法用相对误差核验
            unit = METRIC_BY_KEY[key].unit_type
            if is_pct != (unit in ("percent", "pp")):
                continue
            rows = _db_rows(conn, key, code, year)
            if not rows:
                continue
            best = min(rows, key=lambda r: abs(value - float(r["value"])) /
                       (abs(float(r["value"])) or 1))
            dbv = float(best["value"])
            err = abs(value - dbv) / (abs(dbv) or 1)
            used_spans.append(span)
            if err <= tolerance:
                matched += 1
            else:
                findings.append(Finding(
                    token=m.group(1), value=value, status="suspicious",
                    metric_key=key, metric_name=METRIC_BY_KEY[key].cn_name,
                    db_value=dbv, db_source=best["source"], rel_err=round(err, 4)))

    return {"matched": matched, "suspicious": findings,
            "total": matched + len(findings)}


def format_findings(result: dict) -> str:
    """生成待核实提示文本（无异常返回空串）。"""
    if not result["suspicious"]:
        return ""
    parts = []
    for f in result["suspicious"]:
        parts.append(f"{f.metric_name} {f.token}（库中 {f.db_value:,.2f}，偏差 {f.rel_err:.1%}）")
    return "⚠️ 待核实（与结构化指标库不一致）：" + "；".join(parts)
