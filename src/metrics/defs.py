"""指标定义：规范名 + 别名（锚定行名用）+ 目标表 + 单位类型。

unit_type: currency(元) | percent(百分点) | per_share(元/股) | pp(增减百分点差)
"""
from dataclasses import dataclass, field

# 表类型
S2_DATA = "section2_data"      # 主要会计数据
S2_RATIO = "section2_ratio"    # 主要财务指标
BALANCE = "balance_sheet"
INCOME = "income_stmt"
CASHFLOW = "cashflow"
PROSPECTUS = "prospectus"


@dataclass(frozen=True)
class MetricDef:
    key: str
    cn_name: str
    category: str            # profitability|solvency|cashflow|per_share|scale|growth
    unit_type: str           # currency|percent|per_share|pp
    tables: tuple[str, ...]  # 锚定目标表
    aliases: tuple[str, ...] = ()
    yoy_of: str | None = None  # _yoy 指标的基指标


METRIC_DEFS: list[MetricDef] = [
    # ---- 主要会计数据 (section2_data) ----
    MetricDef("revenue", "营业总收入", "scale", "currency", (S2_DATA, INCOME, PROSPECTUS),
              ("营业总收入", "营业收入", "营业总收入合计", "营业收入合计")),
    MetricDef("net_profit_parent", "归属于母公司股东的净利润", "profitability", "currency", (S2_DATA, INCOME, PROSPECTUS),
              ("归属于上市公司股东的净利润", "归属于母公司所有者的净利润", "归属于母公司股东的净利润", "归母净利润")),
    MetricDef("net_profit_deduct", "扣除非经常性损益后归母净利润", "profitability", "currency", (S2_DATA, INCOME, PROSPECTUS),
              ("归属于上市公司股东的扣除非经常性损益的净利润", "归属于上市公司股东的扣除非经常性损益后的净利润", "扣除非经常性损益后归属于母公司股东的净利润")),
    MetricDef("ocf", "经营活动产生的现金流量净额", "cashflow", "currency", (S2_DATA, CASHFLOW, PROSPECTUS),
              ("经营活动产生的现金流量净额", "经营活动现金流量净额")),
    MetricDef("total_assets", "资产总额", "scale", "currency", (S2_DATA, BALANCE, PROSPECTUS),
              ("资产总计", "资产总额", "总资产")),
    MetricDef("net_assets_parent", "归属于母公司股东的净资产", "scale", "currency", (S2_DATA, BALANCE, PROSPECTUS),
              ("归属于上市公司股东的净资产", "归属于母公司所有者权益合计", "归属于母公司股东的净资产", "归属于母公司所有者权益")),

    # ---- 主要财务指标 (section2_ratio) ----
    MetricDef("eps_basic", "基本每股收益", "per_share", "per_share", (S2_RATIO, INCOME, PROSPECTUS),
              ("基本每股收益", "每股收益")),
    MetricDef("eps_diluted", "稀释每股收益", "per_share", "per_share", (S2_RATIO, INCOME, PROSPECTUS),
              ("稀释每股收益",)),
    MetricDef("eps_basic_deduct", "扣非基本每股收益", "per_share", "per_share", (S2_RATIO, PROSPECTUS),
              ("扣除非经常性损益后的基本每股收益", "扣除非经常性损益的基本每股收益")),
    MetricDef("roe_weighted", "加权平均净资产收益率", "profitability", "percent", (S2_RATIO, PROSPECTUS),
              ("加权平均净资产收益率",)),
    MetricDef("roe_weighted_deduct", "扣非加权平均净资产收益率", "profitability", "percent", (S2_RATIO, PROSPECTUS),
              ("扣除非经常性损益后的加权平均净资产收益率", "扣除非经常性损益的加权平均净资产收益率")),
    MetricDef("bps", "每股净资产", "per_share", "per_share", (S2_RATIO, PROSPECTUS),
              ("归属于上市公司股东的每股净资产", "每股净资产")),

    # ---- 增减幅度派生 (yoy) ----
    MetricDef("revenue_yoy", "营业总收入同比增减", "growth", "percent", (S2_DATA,), ("增减幅度",), yoy_of="revenue"),
    MetricDef("net_profit_parent_yoy", "归母净利润同比增减", "growth", "percent", (S2_DATA,), ("增减幅度",), yoy_of="net_profit_parent"),
    MetricDef("net_profit_deduct_yoy", "扣非净利润同比增减", "growth", "percent", (S2_DATA,), ("增减幅度",), yoy_of="net_profit_deduct"),
    MetricDef("ocf_yoy", "经营现金流净额同比增减", "growth", "percent", (S2_DATA,), ("增减幅度",), yoy_of="ocf"),
    MetricDef("total_assets_yoy", "资产总额同比增减", "growth", "percent", (S2_DATA,), ("增减幅度",), yoy_of="total_assets"),
    MetricDef("net_assets_parent_yoy", "归母净资产同比增减", "growth", "percent", (S2_DATA,), ("增减幅度",), yoy_of="net_assets_parent"),
    MetricDef("eps_basic_yoy", "基本每股收益同比增减", "growth", "percent", (S2_RATIO,), ("增减幅度",), yoy_of="eps_basic"),
    MetricDef("bps_yoy", "每股净资产同比增减", "growth", "percent", (S2_RATIO,), ("增减幅度",), yoy_of="bps"),
    # ROE 的增减是百分点差，不是百分比
    MetricDef("roe_weighted_change_pp", "加权平均ROE增减(百分点)", "growth", "pp", (S2_RATIO,), ("增减幅度",), yoy_of="roe_weighted"),
    MetricDef("roe_weighted_deduct_change_pp", "扣非加权ROE增减(百分点)", "growth", "pp", (S2_RATIO,), ("增减幅度",), yoy_of="roe_weighted_deduct"),

    # ---- 合并资产负债表 ----
    MetricDef("current_assets", "流动资产合计", "scale", "currency", (BALANCE, PROSPECTUS), ("流动资产合计",)),
    MetricDef("monetary_funds", "货币资金", "scale", "currency", (BALANCE, PROSPECTUS), ("货币资金",)),
    MetricDef("accounts_receivable", "应收账款", "scale", "currency", (BALANCE, PROSPECTUS), ("应收账款",)),
    MetricDef("inventory", "存货", "scale", "currency", (BALANCE, PROSPECTUS), ("存货",)),
    MetricDef("total_liabilities", "负债合计", "solvency", "currency", (BALANCE, PROSPECTUS), ("负债合计", "负债总计")),
    MetricDef("current_liabilities", "流动负债合计", "solvency", "currency", (BALANCE, PROSPECTUS), ("流动负债合计",)),
    MetricDef("noncurrent_liabilities", "非流动负债合计", "solvency", "currency", (BALANCE, PROSPECTUS), ("非流动负债合计",)),
    MetricDef("equity_total", "所有者权益合计", "scale", "currency", (BALANCE, PROSPECTUS),
              ("所有者权益合计", "股东权益合计", "归属于母公司所有者权益合计与少数股东权益合计", "所有者权益总计")),
    MetricDef("asset_liability_ratio", "资产负债率", "solvency", "percent", (BALANCE, S2_RATIO),
              ("资产负债率",)),
    # 资产负债表常用科目（扩展：财报问答高频明细项）
    MetricDef("short_loan", "短期借款", "solvency", "currency", (BALANCE, PROSPECTUS), ("短期借款",)),
    MetricDef("notes_payable", "应付票据", "solvency", "currency", (BALANCE, PROSPECTUS), ("应付票据",)),
    MetricDef("accounts_payable", "应付账款", "solvency", "currency", (BALANCE, PROSPECTUS), ("应付账款",)),
    MetricDef("contract_liabilities", "合同负债", "solvency", "currency", (BALANCE, PROSPECTUS), ("合同负债",)),
    MetricDef("advance_receipts", "预收款项", "solvency", "currency", (BALANCE, PROSPECTUS), ("预收款项", "预收账款")),
    MetricDef("deferred_income", "递延收益", "solvency", "currency", (BALANCE, PROSPECTUS), ("递延收益",)),
    MetricDef("long_loan", "长期借款", "solvency", "currency", (BALANCE, PROSPECTUS), ("长期借款",)),
    MetricDef("fixed_assets", "固定资产", "scale", "currency", (BALANCE, PROSPECTUS), ("固定资产",)),
    MetricDef("construction_in_progress", "在建工程", "scale", "currency", (BALANCE, PROSPECTUS), ("在建工程",)),
    MetricDef("intangible_assets", "无形资产", "scale", "currency", (BALANCE, PROSPECTUS), ("无形资产",)),
    MetricDef("goodwill", "商誉", "scale", "currency", (BALANCE, PROSPECTUS), ("商誉",)),

    # ---- 合并利润表 ----
    MetricDef("operating_cost", "营业成本", "profitability", "currency", (INCOME, PROSPECTUS), ("营业成本", "营业总成本")),
    MetricDef("sales_expense", "销售费用", "profitability", "currency", (INCOME, PROSPECTUS), ("销售费用",)),
    MetricDef("admin_expense", "管理费用", "profitability", "currency", (INCOME, PROSPECTUS), ("管理费用",)),
    MetricDef("finance_expense", "财务费用", "profitability", "currency", (INCOME, PROSPECTUS), ("财务费用",)),
    MetricDef("rd_expense", "研发费用", "profitability", "currency", (INCOME, PROSPECTUS),
              ("研发费用", "研发投入", "研发投入合计")),
    MetricDef("operating_profit", "营业利润", "profitability", "currency", (INCOME, PROSPECTUS), ("营业利润",)),
    MetricDef("total_profit", "利润总额", "profitability", "currency", (INCOME, PROSPECTUS), ("利润总额",)),
    MetricDef("net_profit", "净利润", "profitability", "currency", (INCOME, PROSPECTUS), ("净利润",)),

    # ---- 合并现金流量表 ----
    MetricDef("icf", "投资活动产生的现金流量净额", "cashflow", "currency", (CASHFLOW, PROSPECTUS),
              ("投资活动产生的现金流量净额", "投资活动现金流量净额")),
    MetricDef("fcf", "筹资活动产生的现金流量净额", "cashflow", "currency", (CASHFLOW, PROSPECTUS),
              ("筹资活动产生的现金流量净额", "筹资活动现金流量净额")),

    # ---- API 专用指标（baostock/akshare/datasinking，无 PDF 锚定表）----
    MetricDef("gross_margin", "毛利率", "profitability", "percent", (), ("毛利率",)),
    MetricDef("net_margin", "净利率", "profitability", "percent", (), ("净利率",)),
    MetricDef("current_ratio", "流动比率", "solvency", "ratio", (), ("流动比率",)),
    MetricDef("quick_ratio", "速动比率", "solvency", "ratio", (), ("速动比率",)),
    MetricDef("cash_ratio", "现金比率", "solvency", "ratio", (), ("现金比率",)),
    MetricDef("equity_multiplier", "权益乘数", "solvency", "ratio", (), ("权益乘数",)),
    MetricDef("asset_turnover", "总资产周转率", "operation", "ratio", (), ("总资产周转率",)),
    MetricDef("cfo_to_np", "经营现金流/净利润", "cashflow", "ratio", (), ("经营现金流净额/净利润",)),
    MetricDef("eps_ttm", "每股收益TTM", "per_share", "per_share", (), ("每股收益TTM",)),
    MetricDef("roe_dupont", "杜邦ROE", "profitability", "percent", (), ("杜邦ROE",)),
    MetricDef("yoy_net_profit", "净利润同比增长率", "growth", "percent", (), ("净利润同比增长率",)),
    MetricDef("yoy_net_profit_parent", "归母净利润同比增长率", "growth", "percent", (), ("归母净利润同比增长率",)),
    MetricDef("yoy_eps_basic", "基本每股收益同比增长率", "growth", "percent", (), ("基本每股收益同比增长率",)),
    MetricDef("yoy_total_assets", "总资产同比增长率", "growth", "percent", (), ("总资产同比增长率",)),
    MetricDef("yoy_equity", "净资产同比增长率", "growth", "percent", (), ("净资产同比增长率",)),
    MetricDef("total_share", "总股本", "scale", "share", (), ("总股本",)),
    MetricDef("liqa_share", "流通股本", "scale", "share", (), ("流通股本",)),
]

METRIC_BY_KEY: dict[str, MetricDef] = {m.key: m for m in METRIC_DEFS}

# 单位归一化倍数（S4）
UNIT_FACTORS = {"元": 1.0, "千元": 1e3, "万元": 1e4, "百万元": 1e6, "亿元": 1e8}


def norm_row_name(s: str) -> str:
    """行名归一化：全角→半角、去空白、去行首编号（一、五、）、去末尾括号说明（（%）（元/股）等），
    用于锚定匹配。"""
    import re
    t = s.strip()
    # 全角转半角
    out = []
    for ch in t:
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        elif code == 0x3000:
            out.append(" ")
        else:
            out.append(ch)
    t = "".join(out)
    t = re.sub(r"\s+", "", t)
    t = t.replace("（", "(").replace("）", ")")
    # 去行首编号：五、净利润… → 净利润…
    t = re.sub(r"^[一二三四五六七八九十]+[、.．]", "", t)
    # 去末尾括号说明：加权平均净资产收益率（%）/ 基本每股收益（元/股）
    t = re.sub(r"\([^()]{0,12}\)$", "", t)
    # 去中间固定变体：所有者权益（或股东权益）合计 → 所有者权益合计
    t = t.replace("（或股东权益）", "").replace("(或股东权益)", "")
    t = t.rstrip("%％")
    return t
