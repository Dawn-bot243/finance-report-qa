# 上市公司财报智能检索问答 · 阶段一：数据获取与预处理

建立**小规模、高质量、带出处信息**的财报库，为后续 RAG 检索问答打基础。验证基准：[CFQA 中文财报问答数据集](https://github.com/ygan/CFQA)（EPJ Data Science 2025）。

项目各阶段工作报告与最终报告见 [项目报告/](项目报告/)。

## 覆盖范围

CFQA test 集中问题最多的前 10 家公司 × FY2019–2022：

| 公司 | 代码 | CFQA 问答 | 备注 |
|------|------|-----------|------|
| 两面针 | 600249 | 104 | 沪主板 |
| 昊海生科 | 688366 | 92 | 科创板 |
| 芯海科技 | 688595 | 92 | 2020-09 上市，2019 用招股说明书 |
| 建龙微纳 | 688357 | 88 | 科创板 |
| 恒玄科技 | 688608 | 76 | 2020-12 上市，2019 用招股说明书 |
| 伊泰B股 | 900948 | 76 | B 股 |
| 一心堂 | 002727 | 60 | 深市主板 |
| 瑞联新材 | 688550 | 56 | 2020-09 上市，2019 用招股说明书 |
| 泰瑞机器 | 603289 | 52 | 沪主板 |
| 华润双鹤 | 600062 | 44 | 沪主板 |

共 44 份文档（40 份主文档 + 4 份候选版本）、10,683 页。PDF 来源：巨潮资讯网（cninfo）官方披露渠道。

CFQA 数据集噪声处理：`答案出自` 为空的题（165 条）、公司名与代码错配的题（32 条，如 688550 代码下混入豪森股份）、答案为「未披露」类的题（34 条）不参与页码核验；另有 43 条题的答案数字不在所下载 PDF 中（数据源版本/口径差异，如伊泰 B 股题引用 H 股报告），单独报告。

## 安装

```bash
pip install -r requirements.txt   # requests, pandas, pymupdf, pyyaml, baostock, datasinking
python -X utf8 scripts/fetch_cfqa.py   # 下载 CFQA 数据集到 raw/cfqa/（仓库不随附原始数据）
```

Python ≥ 3.10。Windows + Git Bash 环境统一使用 `python -X utf8`。
DataSinking 需要 API key：在 [datasink.ing](https://datasink.ing) 免费注册后写入 `.env`（`DATASINK_API_KEY=...`，已 gitignore）。

## 运行（幂等，可重跑）

```bash
python -X utf8 run_all.py init       # 初始化 SQLite schema + 指标定义
python -X utf8 run_all.py cfqa       # 导入 CFQA 前5家 452 条问答
python -X utf8 run_all.py download   # cninfo 下载年报/招股书 PDF（含候选版本）
python -X utf8 run_all.py text       # PDF 逐页文本化入库（与 CFQA 页码对齐，用于引用溯源）
python -X utf8 run_all.py apis       # API 结构化数据：baostock 指标 + 东财三张表 + datasinking Markdown/指标
python -X utf8 run_all.py metrics    # PDF 结构化指标提取（作为 API 方案的对比基线）+ 交叉校验
python -X utf8 run_all.py qc         # docmap + 四道质量门 + PDF vs API 对比报告
python -X utf8 run_all.py all        # 顺序执行全部
```

常用参数：`--force` 强制重跑、`--only 600249` 只处理某公司、`download --list-missing` 查缺。

## 阶段二：RAG 检索问答（混合检索 + 带出处回答）

```bash
python -X utf8 run_all.py rag build                 # 建索引：pages→chunks + Chroma 向量 + BM25（幂等增量）
python -X utf8 run_all.py rag ask --question "两面针2019年营业收入是多少"
python -X utf8 run_all.py rag eval                  # CFQA 全量评估（检索命中率+数值判分+LLM判分）
python -X utf8 run_all.py rag eval --eval-limit 20 --skip-judge   # 快速小样本评估
```

问答链路：**查询分析（公司/年份/指标）→ 结构化直答（metrics 表，as_reported 优先）→ 混合召回（Chroma bge-m3 + jieba BM25 → RRF）→ bge-reranker 重排 → DeepSeek 生成（行内引用【第N页】+ 数值核验）**。证据不足时拒答。

API 依赖（.env）：`DEEPSEEK_API_KEY`（生成/评测）、`SILICONFLOW_API_KEY`（bge-m3 嵌入 + bge-reranker 重排）、`DATASINK_API_KEY`（阶段一用）。

## 阶段三：指标解释与同业对比

```bash
python -X utf8 run_all.py analysis kb --question "ROE是什么意思"          # 指标知识库（FAQ）
python -X utf8 run_all.py analysis compare --codes 600249,688366 --year 2022   # 同业对比
python -X utf8 run_all.py analysis compare --codes 600249,688366 --year 2022 \
       --metrics revenue,net_profit_parent,roe_weighted                    # 自选指标
python -X utf8 run_all.py analysis verify --text "两面针2022年营业收入为6.9亿元" \
       --only 600249 --year 2022                                           # 数值核验
```

- **指标解释**：`data/metric_kb.yaml` 收录 22 个核心指标（定义/公式/解读/注意事项）。问答入口优先走 FAQ 快速通道（`路径：faq`），命中直接返回，不检索不生成
- **同业对比**：选 2–3 家公司 + 同一报告期，从结构化指标库生成对比表（Markdown + CSV）。as_reported 原文披露优先、API 重述兜底；逐项标注缺失/口径混合/招股书数据（上市前）等可比性问题
- **数值核验**：问答回答中的「指标=数值」陈述与结构化库原值比对（容差 0.5%），不一致时在回答末尾追加「⚠️ 待核实」，并记录在 `structured_check`。只核验可归因的指标陈述，避免叙述性数字误报

阶段三同时修复了招股书数据问题：行名自带单位（`营业收入（万元）`）此前被忽略导致数值缩水 1 万倍；招股书叙述文本触发正则兜底导致错年份/错单位提取（现已对招股书关闭正则兜底），并排除 `2020年1-9月` 等非完整财年列。

## 阶段四：Web 应用与评估

```bash
# 终端 1：后端（FastAPI）
python -X utf8 run_all.py serve --port 8000

# 终端 2：前端（Streamlit）
streamlit run app/streamlit_app.py
# 浏览器打开 http://localhost:8501
```

后端接口：

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 健康检查（文档/页数/指标数） |
| GET | `/companies` | 公司列表 |
| POST | `/ask` | 问答 `{question, top_k}`，返回路径/回答/引用/数值核验 |
| GET | `/kb?question=` | 指标解释（知识库） |
| GET | `/metrics?code=&year=&keys=` | 单公司单财年指标（含出处） |
| GET | `/compare?codes=&year=&metrics=` | 同业对比（含 markdown 与结构化数据） |
| GET | `/metric_defs` | 指标定义列表 |

前端三个页签：财报问答（示例问题、路径提示、引用展开）、指标查询（表格 + 出处）、同业对比（2–3 家公司、指标自选、Markdown 表格 + CSV 下载）。

API 冒烟与延迟测试（进程内，无需先启动服务）：

```bash
python -X utf8 scripts/api_smoke.py
```

## 阶段五：工具调用型 Agent（Agentic RAG）

把检索能力封装为 LLM 可调用的工具，由模型自主决定何时调用哪个工具（单轮多步，最多 6 轮）。

```bash
python -X utf8 run_all.py agent ask --question "对比两面针和一心堂2022年的盈利能力并解释差异原因" --verbose
python -X utf8 run_all.py agent eval              # CFQA 分层抽样 80 题 + 10 条手写多步任务
```

| 工具 | 能力 |
|------|------|
| `search_report` | 全文混合检索 + 带页码引用生成 |
| `get_metric` | 结构化指标点查（含出处） |
| `compare_companies` | 2–3 家公司同报告期对比 |
| `explain_metric` | 指标知识库解释（22 个指标） |
| `verify_numbers` | 数值核验（容差 0.5%） |

实现：手写 function-calling 循环（requests 直连 DeepSeek tools 参数，不引入 Agent 框架）——
[src/agent/tools.py](src/agent/tools.py) 工具 schema 与实现、[src/agent/loop.py](src/agent/loop.py) 循环与 trace、
[src/agent/eval.py](src/agent/eval.py) 评估。`--verbose` 打印每步工具调用与耗时。

## 结构化指标的三条来源

按「不用 PDF 硬提表格」原则，结构化指标以 API 为主：

| 来源 | 内容 | 备注 |
|------|------|------|
| baostock（免费无key） | 盈利能力/偿债/现金流/成长/营运/杜邦六组指标 + 净利润/营收绝对值 | 无三张表全科目行 |
| 东方财富数据中心（直连 JSON） | 三张表主要科目（总资产/负债/货币资金/归母净利润/扣非归母/经营现金流等，元） | 原计划用 akshare 封装，其 HTML 抓取已失效，改为直连底层 API |
| DataSinking（需 API key，放 `.env` 的 `DATASINK_API_KEY`） | 年报全文干净 Markdown（入 report_markdown 表，供 RAG）+ 主要会计数据/财务指标表解析 | 覆盖有限：建龙微纳、恒玄科技无 2019-2022 历史索引 |

PDF 提取（`metrics` 步骤）保留作为对比基线，出处信息链（文档+页码+原文片段）不变。质检报告含「PDF vs API 质量对比」章节。

## 口径维度与出处回填

- `metrics.basis`：**as_reported**（年报原文披露值：PDF 提取 + datasinking，与 CFQA 标准答案同口径）/ **restated**（API 追溯重述值：baostock + 东财）。检索问答时优先取 as_reported 回答 CFQA 问题
- `metrics.evidence_doc_id / evidence_page_no`：API 行与 PDF 原文值在容差内一致时，回填年报文档+页码出处；`cross_metric_id/cross_status` 记录与 PDF 行的匹配关系（match/mismatch）。数值不符的 API 行只链接不给出处（避免引错页），在质检报告 mismatch 清单里列示

## 目录结构

```
run_all.py            单入口 CLI
config.yaml           公司清单/招股书回退/下载参数/质检阈值
src/
  downloader/         cninfo 客户端 + 下载编排（过滤摘要、更正版优先、招股书回退）
  text/               pymupdf 逐页文本+表格提取、页眉页脚清洗
  metrics/            指标定义/单位解析/表分类/锚定取值/主编排/交叉校验
  store/              SQLite schema + 连接（WAL、外键、幂等 upsert）
  cfqa/               数据集导入（公司名核对）
  qa/                 docmap（问题→文档映射、招股书定主）+ 四道质量门 + 报告
scripts/inspect_pdf.py  调试工具：dump 指定页的 blocks/表格
raw/cfqa/            CFQA 原始数据（只读）
raw/pdf/{code}/      下载的 PDF（含 sha256 校验）
db/finance.db        SQLite 库
reports/             质检报告 MD + 提取率矩阵 CSV
logs/                运行日志
```

## 数据库（db/finance.db）

- `companies` / `documents` / `pages` / `metric_defs` / `metrics` / `cfqa_questions` / `download_log` / `qc_runs`
- **出处信息链**：`metrics → (doc_id, page_no, source_text 原文片段) → documents.file_path → pages.text`
- 页码按 PDF 文档页序 1 起始，与 CFQA `答案出自` 对齐
- 指标值归一化：货币→元、比率→百分点、每股→元/股；`raw_value/raw_unit` 保留原始形态

## 质检（四道门）

1. **Gate1 文档完整性**：下载状态、sha256 唯一、页数范围、CFQA 页码不越界
2. **Gate2 答案命中**：答案文本在 `答案出自` 页码组中的命中率 ≥85%（空页码条跳过）
3. **Gate3 指标交叉**：section2 vs 三大报表 / 跨年上年同期 vs 上年本期 / 招股书 vs 次年报告，容差 0.5%
4. **Gate4 提取率**：第二节核心 12 指标 ≥90%、三大报表核心 16 行 ≥85%

详见 `reports/` 下最新报告。CFQA 数据噪声说明：test 集 452 条中 131 条 `答案出自` 为空（如 688366 名下混入「晶丰明源」问题），无法做页码校验，标为 skip。

## 数据出处与合规

- PDF 来自证监会指定信息披露网站巨潮资讯网，仅用于研究与验证用途
- CFQA 数据集遵循其仓库 [LICENSE](https://github.com/ygan/CFQA)（引用见其 README）
