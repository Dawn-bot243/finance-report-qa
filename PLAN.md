# 阶段一：财报库数据获取与预处理 — 实施计划

## Context

为「A股上市公司公告/财报智能检索问答系统」建立小规模、高质量、带出处信息的财报库。验证基准是 CFQA 中文财报问答数据集（ygan/CFQA），覆盖其 test 集中问题最多的前 5 家公司 × 2019–2022 四个财年。结构化指标必须从 PDF 本身解析提取（PDF 是数据源头），出处信息链贯穿始终（指标→文档→页码→原文片段）。

**用户已确认的决策**：A股市场；前5家试点公司；核心指标集（30–50个）；Python 3.13 + SQLite；PDF 来自巨潮资讯网。

## 已验证的关键事实（实现时直接采信，勿重验）

1. **CFQA 数据**已下载到 `raw/cfqa/`（train/dev/test 三个 JSON，共 10,311 条）。字段：股票代码/公司/问题/答案/答案出自/id。
   - `答案出自` 是页码组列表（如 `[[6],[154]]`），页码按 PDF 文档页序 1 起始，非印刷页码。
   - 前5家共 452 条问答，其中 **131 条 `答案出自` 为空数组**（数据集噪声，如 688366 昊海生科名下混入「晶丰明源」问题）——质检时标 `skip` 单独报告，不参与页码校验。
2. **前5家**（按问答条数）：两面针 600249(104条)、昊海生科 688366(92)、芯海科技 688595(92)、建龙微纳 688357(88)、恒玄科技 688608(76)。
3. **恒玄（2020-12上市）与芯海（2020-09上市）无 2019 年报**，2019 年问题的出处是招股说明书，均在 cninfo 上：
   - 恒玄：`finalpage/2020-12-09/1208870808.PDF`（招股说明书注册稿）；备选 `finalpage/2020-11-25/1208793582.PDF`（招股意向书）
   - 芯海：`finalpage/2020-09-22/1208465054.PDF`（招股说明书）
   - 因此文档总量 = 18 份年报 + 2 份招股书 = 20 份 PDF
4. **cninfo 接口**（已实测）：
   - orgId 查询：`POST http://www.cninfo.com.cn/new/information/topSearch/query`，data=`keyWord=<公司名>&maxNum=10` → `[{code,orgId,zwjc,category}]`。已查：两面针 gssh0600249、昊海生科 gssh0688366、建龙微纳 gssh0688357、恒玄 nssc1000367、芯海 gfbj0837517（后两家是拟上市/股转注册，正常）
   - 公告查询：`POST http://www.cninfo.com.cn/new/hisAnnouncement/query`，data 含 `stock=<code>,<orgId>`、`category=category_ndbg_szsh;`（年度报告）、`seDate=YYYY-01-01~YYYY+1-06-30`、`column=sse`、`tabName=fulltext`、`isHLtitle=true`
   - PDF 下载：`https://static.cninfo.com.cn/<adjunctUrl>`，无需鉴权、支持 Range。标题含「摘要」的排除
   - 务必带浏览器 UA；requests 库构造表单（不要 curl 裸传中文，编码易错）
5. **环境**：Python 3.13.9 已装 requests/pandas，需 `pip install pymupdf`。Windows + Git Bash：所有命令用 `python -X utf8`，代码内显式 `encoding='utf-8'`，避免 shell 间传中文路径。

## 项目结构

```
D:\金融项目\
├── README.md                 # 安装/运行/数据出处/质检报告索引
├── requirements.txt          # requests, pandas, pymupdf>=1.24
├── config.yaml               # 5家公司清单、招股书回退配置、路径、下载参数、质检阈值
├── run_all.py                # 单入口 CLI：init|cfqa|download|text|metrics|qc|all（幂等，--force 重跑）
├── src/
│   ├── paths.py              # project_root() 用 __file__ 定位（不依赖 cwd）
│   ├── config.py  logutil.py
│   ├── downloader/
│   │   ├── cninfo.py         # CninfoClient：find_org(代码+名称双核对)、query_announcements、download_pdf(重试/原子改名)
│   │   └── download.py       # 公司×年份→候选公告→过滤摘要/英文版→更正版优先→sha256→招股书回退
│   ├── text/pdftext.py       # pymupdf 逐页提取：blocks(y,x)排序处理多栏、页眉页脚清洗、表格渲染行
│   ├── metrics/
│   │   ├── defs.py           # ~44 个 MetricDef（规范名/别名/锚定正则/目标表/单位类型）
│   │   ├── units.py          # 「单位：元/万元…」解析归一化
│   │   ├── tabparse.py       # find_tables 清洗、表分类、列结构识别（本期/上年同期/增减幅度；招股书多年并列）
│   │   ├── anchor.py         # 行名归一化、parse_number 白盒规则、正则兜底
│   │   └── extract.py        # 主编排 + 交叉校验
│   ├── store/
│   │   ├── schema.sql  db.py（WAL、外键ON、幂等 upsert） loader.py
│   ├── cfqa/importer.py      # 三个 split JSON → companies/cfqa_questions（按5家过滤、公司名核对）
│   └── qa/
│       ├── docmap.py         # 问题→文档映射；招股书多候选时按答案命中率选优（is_primary）
│       ├── qc.py             # 四道质量门
│       └── report.py         # 质检报告 MD + 提取率矩阵 CSV
├── scripts/inspect_pdf.py    # 调试工具：dump 指定 PDF 指定页的文本/表格/区块
├── raw/cfqa/（已有，只读）  raw/pdf/{code}/
├── db/finance.db  reports/  logs/
```

## SQLite 表（src/store/schema.sql）

- `companies(code PK, name, org_id, market, listed_date, note)`
- `documents(doc_id PK, code FK, fiscal_year, doc_type[annual_report|prospectus], title, ann_time, adjunct_url UNIQUE, final_url, file_path, file_size, sha256, page_count, is_primary, download_status, download_error)`
- `pages(page_id PK, doc_id FK, page_no, text, tables_json, n_chars, UNIQUE(doc_id,page_no))`
- `metric_defs(key PK, cn_name, category, unit_type)`
- `metrics(metric_id PK, code FK, fiscal_year, metric_key FK, value, raw_value, raw_unit, source[section2|income_stmt|balance_sheet|cashflow|prospectus], doc_id FK, page_no, source_text, column_label[本期|上年同期|20XX年度], method[table|regex_fallback], confidence, cross_metric_id, cross_status, UNIQUE(code,fiscal_year,metric_key,source,column_label))`
- `cfqa_questions(question_id PK, code FK, company_name, split, question, answer, answer_pages, q_year, qa_doc_map, qa_check)`
- `download_log`, `qc_runs`

出处信息链：metrics → (doc_id, page_no, source_text) → documents.file_path → pages.text。FTS5 全文检索留到阶段二（中文需 jieba/trigram 方案，不阻塞阶段一）。

## 指标提取算法（核心难点，八步）

指标集约 44 个：第二节「主要会计数据」「主要财务指标」两表全取 + 增减幅度列派生 `_yoy`；合并资产负债表/利润表/现金流量表核心行（总资产、归母净资产、负债合计、资产负债率、营业收入、归母净利润、扣非净利润、加权平均ROE、基本/稀释EPS、每股净资产、经营/投资/筹资现金流净额等）。

1. **S1 定位**：逐页匹配 `第X节 公司简介和主要财务指标`，排除目录页（页≤8 且行尾页码/省略号），搜索窗口 [start, start+8]；失败则全文搜「主要会计数据」
2. **S2 取表**：窗口页 `page.find_tables()`；有字样无表 → 该页转正则兜底
3. **S3 分类**：表特征匹配 → section2_data / section2_ratio / balance_sheet / income_stmt / cashflow / prospectus（列头为多年并列）；同页同类取行数最多
4. **S4 单位**：表内「单位：X」行 → 倍数{元:1,千元:1e3,万元:1e4,百万元:1e6,亿元:1e8}；表内无则取表上方最近行（≤60pt）；再无默认元记 `assumed`
5. **S5 列识别**：表头正则 `本期|上年同期|增减幅度|(%)` → col_map；招股书列头 `20\d\d年(度|末)?` 映射财年；失败转正则兜底
6. **S6 锚定**：行名归一化（全角→半角、去空格换行括号）后按别名匹配取「本期」列；上年同期列另存一条 fiscal_year−1 记录。`parse_number()`：千分位/全角/括号负数/「不适用」→None/主数值正则；百分比存百分点（6.38 非 0.0638）；货币归一为元
7. **S7 正则兜底**：`别名\s*[：:]?\s*(数值)`，confidence=low，已有 table 值不重复
8. **S8 交叉校验**（自动，不覆盖，差异并存报告）：①同文档 section2 vs 三大报表；②跨文档 Y年「上年同期」vs Y−1年「本期」；③招股书值 vs 2020年报上年同期列。相对误差 <0.5% 记 match

落库幂等键 UNIQUE(code,fiscal_year,metric_key,source,column_label)。

## 四道质量门（qa/qc.py，qc_runs 留痕）

1. **Gate1 文档完整性**：20 份 PDF 下载 ok、sha256 唯一、page_count∈[50,900]；CFQA 页码组中每页 ≤ 对应文档 page_count（2019 恒玄/芯海映射到招股书）
2. **Gate2 答案命中**：答案归一化（去空白/标点/公司名前缀）后在页码组任一页 text 中命中；按公司×年份统计命中率（目标 ≥85%）；空页码 131 条标 skip 单独报告；招股书多候选按命中率选 is_primary
3. **Gate3 指标交叉验证**：S8 差异表，mismatch 全量列报告，人工抽查 ≥10 条
4. **Gate4 提取率**：metric_defs × 20 文档矩阵 CSV/MD，第二节核心12指标 ≥90%、三大报表核心行 ≥85%；缺失分类「确实未披露」vs「算法漏提」

## 实现顺序

1. **脚手架**：paths/config/logutil/schema.sql/db.py/requirements/README + `run_all.py init`
2. **CFQA 导入**：importer 过滤 5 家 452 条入 `cfqa_questions`
3. **下载器**：先跑两面针（沪主板格式最标准）验证 → 全量 18 年报 + 2 招股书（含候选版本，docmap 后定主）
4. **PDF 文本化 + 早期质检**：入库后**立即跑 Gate1+Gate2**——先验证「CFQA 页码=文档页序」对齐假设，再建指标层
5. **指标提取**（迭代式）：两面针 4 份年报跑通 S1–S8 → 泛化到 3 家科创板 → 最后招股书特例；inspect_pdf.py 逐页调试
6. **终检**：Gate2/3/4 全量、质检报告、提取率矩阵、`run_all.py all` 复跑确认幂等

## 关键风险与对策

- **CFQA 用的 PDF 版本可能与我们下载的不一致**（页码偏移）：documents 允许同 (code,year) 多候选并存，docmap 按 Gate2 命中率切 is_primary
- **表格提取失效**（合并单元格/跨页断表）：find_tables + 正则兜底双路径；inspect_pdf.py 调试
- **单位歧义错 1000 倍**：逐表关联单位 + S8 交叉校验兜底暴露
- **cninfo 限流**：1.5s 间隔、浏览器 UA、3 次指数退避、失败写 download_log 可续跑
- **页眉页脚污染**：只剔除 ≥60% 页面出现且 <60 字符的完全重复行 + 纯页码行，Gate2 反向验证清洗未过度

## 验证方案（端到端）

1. `pip install pymupdf` → `python -X utf8 run_all.py init cfqa download`：20 份 PDF 落盘，documents 表 20 行
2. `python -X utf8 run_all.py text`：pages 表总页数与 PDF 实际页数一致；抽查 1 份 PDF 的 page 1/50/100 文本
3. `python -X utf8 run_all.py metrics qc`：四道门报告；Gate2 命中率 ≥85%（空页码条除外）；Gate4 提取率矩阵
4. 人工抽查：质检报告列出的 mismatch 与缺失项 ≥10 条，对照 reports/ 中的原文片段核 PDF
5. `python -X utf8 run_all.py all` 复跑一遍：无错误、幂等（skip 全部已有记录）
