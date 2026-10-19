"""财报智能问答与同业对比 · Streamlit 前端。

先启动后端：
    python -X utf8 run_all.py serve --port 8000
再启动本界面：
    streamlit run app/streamlit_app.py
"""
import csv
import io
import os

import pandas as pd
import requests
import streamlit as st

API = os.getenv("API_BASE_URL", "http://127.0.0.1:8000")
API_KEY = os.getenv("API_KEY", "")
HEADERS = {"X-API-Key": API_KEY} if API_KEY else {}

st.set_page_config(page_title="财报智能问答", page_icon="📈", layout="wide")


@st.cache_data(ttl=60)
def api_get(path: str, **params):
    r = requests.get(f"{API}{path}", params=params, headers=HEADERS, timeout=60)
    r.raise_for_status()
    return r.json()


def api_post(path: str, payload: dict):
    r = requests.post(f"{API}{path}", json=payload, headers=HEADERS, timeout=180)
    r.raise_for_status()
    return r.json()


def health() -> dict | None:
    try:
        return api_get("/health")
    except Exception:
        return None


st.title("📈 上市公司财报智能问答")
st.caption("检索财报原文 · 生成带出处回答 · 指标解释 · 同业对比")

health_info = health()
if health_info is None:
    st.error(f"无法连接后端 {API}。请先运行：`python -X utf8 run_all.py serve --port 8000`")
    st.stop()
with st.sidebar:
    st.subheader("系统状态")
    st.success("后端已连接")
    st.write(f"文档 {health_info['documents']} 份 · 页面 {health_info['pages']:,} · 指标 {health_info['metrics']:,} 条")
    st.divider()
    st.caption("数据来源：巨潮资讯网年报/招股书\n\n模型：DeepSeek · bge-m3 · bge-reranker")

tab_ask, tab_metrics, tab_compare, tab_agent = st.tabs(
    ["💬 财报问答", "📊 指标查询", "⚖️ 同业对比", "🤖 智能体问答"])

# ---------------- 问答 ----------------
with tab_ask:
    examples = [
        "两面针2019年营业收入是多少",
        "毛利率是什么",
        "恒玄科技2020年研发投入情况",
        "昊海生科2021年在医美方面有哪些布局",
    ]
    # 用 session_state 承载输入，示例按钮只写状态（避免 text_area 的 value 在重跑时被重置）
    if "ask_question" not in st.session_state:
        st.session_state.ask_question = ""
    cols = st.columns(len(examples))
    for col, ex in zip(cols, examples):
        if col.button(ex, width="stretch"):
            st.session_state.ask_question = ex

    with st.form("ask_form", clear_on_submit=False):
        question = st.text_area("问题", key="ask_question", height=80,
                                placeholder="例如：两面针2022年资产负债率是多少？")
        submitted = st.form_submit_button("提问", type="primary")

    if submitted:
        if not question.strip():
            st.warning("请输入问题后再点击提问")
        else:
            res = None
            with st.spinner("检索并生成回答…"):
                try:
                    res = api_post("/ask", {"question": question.strip()})
                except Exception as e:
                    st.error(f"请求失败：{e}")
            if res:
                path_labels = {"faq": "指标解释（知识库直达）", "structured": "结构化指标直答",
                               "rag": "检索增强生成"}
                st.info(f"回答路径：**{path_labels.get(res['path'], res['path'])}**")
                st.markdown(res["answer"])
                if res.get("structured_check", {}).get("suspicious_tokens"):
                    st.warning("数值核验发现待核实项：" + "、".join(res["structured_check"]["suspicious_tokens"]))
                with st.expander("引用与检索详情"):
                    if res.get("citations"):
                        for c in res["citations"]:
                            st.write("- " + str(c))
                    for ch in res.get("chunks", []):
                        st.caption(f"doc {ch['doc_id']} · 第{ch['page_no']}页 · 相关度 {ch['score']}")

# ---------------- 指标查询 ----------------
with tab_metrics:
    companies = api_get("/companies")
    name_map = {f"{c['name']}（{c['code']}）": c["code"] for c in companies}
    col1, col2 = st.columns([2, 1])
    chosen = col1.selectbox("公司", list(name_map.keys()), key="m_company")
    year = col2.selectbox("报告期", [2022, 2021, 2020, 2019], key="m_year")
    try:
        data = api_get("/metrics", code=name_map[chosen], year=year)
        df = pd.DataFrame([{
            "指标": m["name"],
            "数值": m.get("display", ""),
            "口径": {"as_reported": "原文披露", "restated": "API重述"}.get(m.get("basis"), ""),
            "出处": (f"{'招股书' if m.get('doc_type') == 'prospectus' else '年报'} p{m.get('page_no')}"
                     if m.get("page_no") else (m.get("source") or "")),
        } for m in data["metrics"]])
        st.dataframe(df, width="stretch", hide_index=True)
        st.caption("「未披露」表示该指标在报告中无数据，不视为 0。")
    except Exception as e:
        st.error(f"查询失败：{e}")

# ---------------- 同业对比 ----------------
with tab_compare:
    companies = api_get("/companies")
    labels = {f"{c['name']}（{c['code']}）": c["code"] for c in companies}
    default = list(labels)[:2]
    chosen_labels = st.multiselect("选择 2-3 家公司", list(labels.keys()), default=default, max_selections=3)
    year = st.selectbox("报告期", [2022, 2021, 2020, 2019], key="c_year")
    try:
        defs = api_get("/metric_defs")
        key_name = {d["key"]: d["name"] for d in defs}
    except Exception:
        key_name = {}
    default_keys = ["revenue", "net_profit_parent", "roe_weighted", "ocf"]
    chosen_keys = st.multiselect(
        "对比指标", list(key_name.keys()),
        default=default_keys,
        format_func=lambda k: key_name.get(k, k),
    )
    if st.button("生成对比", type="primary") and 2 <= len(chosen_labels) <= 3:
        codes = ",".join(labels[l] for l in chosen_labels)
        with st.spinner("读取结构化指标库…"):
            try:
                res = api_get("/compare", codes=codes, year=year,
                              metrics=",".join(chosen_keys) if chosen_keys else None)
                st.markdown(res["markdown"])
                # CSV 下载
                buf = io.StringIO()
                w = csv.writer(buf)
                w.writerow(["指标", "公司", "数值", "口径", "来源", "出处页码"])
                for row in res["rows"]:
                    for comp, cell in zip(res["companies"], row["cells"]):
                        w.writerow([row["name"], comp["name"], cell["display"],
                                    cell["basis"] or "", cell["source"] or "",
                                    cell["page_no"] or ""])
                st.download_button("下载 CSV", buf.getvalue().encode("utf-8-sig"),
                                   file_name=f"peer_compare_{year}.csv", mime="text/csv")
            except Exception as e:
                st.error(f"对比失败：{e}")
    elif chosen_labels:
        st.caption("请选择 2-3 家公司后点击「生成对比」。")

# ---------------- 智能体问答 ----------------
with tab_agent:
    st.caption("Agent 会自主决定调用哪个工具（全文检索 / 指标查询 / 同业对比 / 指标解释 / 数值核验），"
               "单次提问最多 6 轮工具调用。复杂问题（对比、归因、多步）效果更好。")

    agent_examples = [
        "对比两面针和一心堂2022年的盈利能力，并解释差异原因",
        "两面针2022年营业收入是多少？和2021年相比是增长还是下降？",
        "ROE是什么意思？两面针2022年的ROE是多少？",
        "帮我核验这句话：两面针2022年营业收入为6.69亿元",
    ]
    if "agent_question" not in st.session_state:
        st.session_state.agent_question = ""
    cols = st.columns(len(agent_examples))
    for i, (col, ex) in enumerate(zip(cols, agent_examples)):
        if col.button(ex, key=f"agent_ex_{i}", width="stretch"):
            st.session_state.agent_question = ex

    with st.form("agent_form", clear_on_submit=False):
        agent_q = st.text_area("问题", key="agent_question", height=80,
                               placeholder="例如：对比两面针和一心堂2022年的盈利能力，并解释差异原因")
        agent_submitted = st.form_submit_button("提问（Agent）", type="primary")

    if agent_submitted:
        if not agent_q.strip():
            st.warning("请输入问题后再点击提问")
        else:
            with st.spinner("Agent 正在规划并调用工具（可能需要 10–60 秒）…"):
                try:
                    st.session_state.agent_result = api_post(
                        "/agent", {"question": agent_q.strip(), "max_iters": 6})
                except Exception as e:
                    st.error(f"请求失败：{e}")
                    st.session_state.agent_result = None

    agent_res = st.session_state.get("agent_result")
    if agent_res:
        trace = agent_res.get("trace", [])
        st.success(f"完成：{agent_res['iterations']} 轮推理 · 工具调用 {len(trace)} 次 · "
                   f"耗时 {agent_res.get('elapsed_s', '?')}s")
        st.markdown(agent_res["answer"])
        if trace:
            tool_cn = {"search_report": "全文检索", "get_metric": "指标查询",
                       "compare_companies": "同业对比", "explain_metric": "指标解释",
                       "verify_numbers": "数值核验"}
            with st.expander(f"工具调用轨迹（{len(trace)} 次）", expanded=False):
                st.dataframe(pd.DataFrame([{
                    "顺序": i + 1,
                    "工具": tool_cn.get(t["tool"], t["tool"]),
                    "参数": t["args"][:120],
                    "结果字数": t["chars"],
                    "耗时(ms)": t["elapsed_ms"],
                    "状态": "✓" if t["ok"] else "✗",
                } for i, t in enumerate(trace)]), width="stretch", hide_index=True)
                st.caption("点击展开可查看每次工具调用的参数与结果摘要。")
