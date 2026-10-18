"""表格解析：find_tables 结果清洗、表分类、列结构识别。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .defs import BALANCE, CASHFLOW, INCOME, PROSPECTUS, S2_DATA, S2_RATIO


@dataclass
class ParsedTable:
    page_no: int
    rows: list[list[str]]
    kind: str | None
    col_map: dict[str, int] = field(default_factory=dict)  # 本期/上年同期/增减幅度/{year}年度 → 列号
    unit_factor: float = 1.0
    unit_word: str = "元"
    unit_source: str = "assumed"


_YEAR_HEADER = re.compile(r"20\d\d\s*年(?:度|末)?")


def _cell(row: list[str], i: int) -> str:
    return str(row[i] or "").strip() if i < len(row) else ""


def _all_text(rows: list[list[str]]) -> str:
    return "".join(str(c or "") for r in rows for c in r)


def classify_table(rows: list[list[str]], doc_type: str = "annual_report") -> str | None:
    """表特征分类。顺序敏感：先第二节标志，再三大报表。招股书分类仅限招股书文档
    （年报的年份列头表格不能误判为招股书）。"""
    # 去全部空白：单元格内换行会把关键词切成「经营活动产生的现金流量 净额」
    text = re.sub(r"\s+", "", _all_text(rows))
    head = text[:200]
    if ("主要会计数据" in head or ("营业总收入" in text and "归属于上市公司股东的净利润" in text
                                   and "经营活动产生的现金流量净额" in text and len(rows) <= 12)):
        return S2_DATA
    if ("主要财务指标" in head or "加权平均净资产收益率" in text and "基本每股收益" in text and len(rows) <= 12
            or "每股净资产" in text and len(rows) <= 10):
        return S2_RATIO
    if "资产总计" in text and "负债合计" in text:
        return BALANCE
    if ("营业总收入" in text or "营业收入" in text) and "营业成本" in text and "净利润" in text:
        return INCOME
    if "经营活动产生的现金流量净额" in text and "投资活动" in text:
        return CASHFLOW
    if doc_type == "prospectus":
        # 招股书：表头含多个并列年份列
        header_line = "".join(rows[0]) + "".join(rows[1]) if len(rows) >= 2 else "".join(rows[0])
        if len(_YEAR_HEADER.findall(header_line)) >= 3:
            return PROSPECTUS
    return None


def _is_header_row(cells: list[str]) -> bool:
    """首行是否为表头/标题行（用于判断跨页续表的拼接）。"""
    s = "".join(cells)
    return bool(re.search(r"项目|单位|主要会计数据|主要财务指标|附注|币种", s)) or len(cells) <= 2


def stitch_tables(page_tables: list[tuple[int, list[list[str]]]]) -> list[tuple[int, list[list[str]]]]:
    """跨页拼接断裂表格：下一页首表首行非表头且列数一致 → 续接上一页末表。
    注意：拼接条件是「上一页的下一页」，要跟踪最后合并页而不是起始页
    （三页连断时 pno == start+2 也必须能续上）。"""
    merged: list[tuple[int, list[list[str]]]] = []
    cur: tuple[int, list[list[str]]] | None = None
    cur_last = 0
    for pno, rows in page_tables:
        if not rows:
            continue
        if (cur is not None and pno == cur_last + 1
                and not _is_header_row(rows[0])
                and len(rows[0]) == len(cur[1][0])):
            cur = (cur[0], cur[1] + rows)
            cur_last = pno
        else:
            if cur is not None:
                merged.append(cur)
            cur = (pno, rows)
            cur_last = pno
    if cur is not None:
        merged.append(cur)
    return merged


def split_segments(rows: list[list[str]]) -> list[list[list[str]]]:
    """拼接表中按表头行切分独立报表段：
    首格为「项目」或含「主要会计数据/主要财务指标」的行开启新段
    （三张报表在拼接表中首尾相连，各自带「项目|附注」表头）。"""
    segments: list[list[list[str]]] = []
    cur: list[list[str]] | None = None
    for row in rows:
        first = str(row[0] or "").strip()
        is_head = (first == "项目"
                   or "主要会计数据" in first
                   or "主要财务指标" in first)
        if is_head:
            cur = [row]
            segments.append(cur)
        elif cur is not None:
            cur.append(row)
    # 无表头的单段回退
    if not segments and rows:
        return [rows]
    return segments


def detect_columns(rows: list[list[str]], kind: str, fiscal_year: int) -> dict[str, int]:
    """识别列结构 → {标签: 列号}。

    - 年报第二节/科创板：列头可能是「2020年/2019年」年份形式（映射为本期/上年同期），
      也可能是「本期/上年同期」；「本期比上年同期增减(%)」归为增减幅度
    - 报表：期末余额/本期金额 → 本期；期初余额/上期金额 → 上年同期
    - 招股书：20XX年度/20XX年末 并列
    """
    col_map: dict[str, int] = {}
    for row in rows[:6]:
        for ci, cell in enumerate(row):
            c = str(cell or "").strip().replace(" ", "")
            if not c:
                continue
            # 增减列优先判断（含「上年同期」字样时不能误判为上年同期列）
            if "增减" in c or "同比" in c:
                col_map.setdefault("增减幅度", ci)
                continue
            if kind == PROSPECTUS:
                m = re.search(r"(20\d\d)\s*年", c)
                if m and m.group(1) not in col_map:
                    # 期间列（2020年1-9月/2020年9月末）不是完整财年，跳过；
                    # 但年末列（2019年12月31日）是完整财年，保留
                    if re.search(r"\d+\s*[-~至]\s*\d+\s*月", c):
                        continue
                    mth = re.search(r"(\d{1,2})\s*月", c)
                    if mth and int(mth.group(1)) != 12:
                        continue
                    col_map[m.group(1)] = ci
                continue
            m = re.search(r"(20\d\d)\s*年", c)
            if m:
                y = m.group(1)
                if int(y) == fiscal_year:
                    col_map.setdefault("本期", ci)
                elif int(y) == fiscal_year - 1:
                    col_map.setdefault("上年同期", ci)
                continue
            if re.match(r"^(本期|本期末|期末余额|本期金额|本期发生额|本期数|本年数?)(?:金额)?$", c):
                col_map.setdefault("本期", ci)
            elif re.match(r"^(上年同期|上期|期初余额|上期金额|上期发生额|上期数|上年数?)(?:金额)?$", c):
                col_map.setdefault("上年同期", ci)
    return col_map


def clean_table(raw_rows: list[list]) -> list[list[str]]:
    """清洗：去全空行、去 None、单元格去换行。"""
    out = []
    for row in raw_rows:
        cells = [str(c or "").replace("\n", " ").strip() for c in row]
        if any(cells):
            out.append(cells)
    return out
