# 上市公司财报智能检索问答 · 阶段一：数据获取与预处理

建立**小规模、高质量、带出处信息**的财报库，为后续 RAG 检索问答打基础。验证基准：[CFQA 中文财报问答数据集](https://github.com/ygan/CFQA)（EPJ Data Science 2025）。

## 覆盖范围

CFQA test 集中问题最多的前 5 家公司 × FY2019–2022：

| 公司 | 代码 | CFQA 问答 | 备注 |
|------|------|-----------|------|
| 两面针 | 600249 | 104 | 沪主板 |
| 昊海生科 | 688366 | 92 | 科创板 |
| 芯海科技 | 688595 | 92 | 2020-09 上市，2019 用招股说明书 |
| 建龙微纳 | 688357 | 88 | 科创板 |
| 恒玄科技 | 688608 | 76 | 2020-12 上市，2019 用招股说明书 |

PDF 来源：巨潮资讯网（cninfo）官方披露渠道。CFQA 数据集本身有噪声（部分条目 `答案出自` 为空、个别问题文不对题），质检时单独标记。

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
