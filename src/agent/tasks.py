"""手写多步任务（评估 Agent 的工具规划能力）。

expected_tools 为「至少应出现的工具」，判分时只检查是否被调用，不强制顺序。
"""
from __future__ import annotations

MULTI_STEP_TASKS: list[dict] = [
    {
        "id": "t1",
        "question": "对比两面针和一心堂2022年的盈利能力，并解释差异原因。",
        "expected_tools": ["compare_companies", "search_report"],
    },
    {
        "id": "t2",
        "question": "两面针2022年和2021年的营业收入分别是多少？同比是增长还是下降？",
        "expected_tools": ["get_metric"],
    },
    {
        "id": "t3",
        "question": "ROE 和毛利率分别是什么意思？两面针2022年这两个指标是多少？",
        "expected_tools": ["explain_metric", "get_metric"],
    },
    {
        "id": "t4",
        "question": "昊海生科2021年的研发投入情况如何？",
        "expected_tools": ["search_report"],
    },
    {
        "id": "t5",
        "question": "对比芯海科技和恒玄科技2019年的营业收入。",
        "expected_tools": ["compare_companies"],
    },
    {
        "id": "t6",
        "question": "帮我核验这句话：两面针2022年营业收入为6.69亿元。",
        "expected_tools": ["verify_numbers"],
    },
    {
        "id": "t7",
        "question": "建龙微纳2020年的归母净利润是多少？请给出年报页码出处。",
        "expected_tools": ["get_metric"],
    },
    {
        "id": "t8",
        "question": "伊泰B股2022年的经营现金流净额和资产负债率分别是多少？",
        "expected_tools": ["get_metric"],
    },
    {
        "id": "t9",
        "question": "泰瑞机器2021年和2022年的营业收入有什么变化？",
        "expected_tools": ["get_metric"],
    },
    {
        "id": "t10",
        "question": "瑞联新材2020年主要产品的营业收入构成是怎样的？",
        "expected_tools": ["search_report"],
    },
]
