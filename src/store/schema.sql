-- 财报库 schema：出处信息链 metrics → (doc_id, page_no, source_text) → documents.file_path → pages.text

CREATE TABLE IF NOT EXISTS companies (
    code        TEXT PRIMARY KEY,               -- 600249
    name        TEXT NOT NULL,                  -- CFQA 口径公司名
    org_id      TEXT,                           -- gssh0600249
    market      TEXT,                           -- sse
    listed_date TEXT,                           -- 上市日（判断上市前年份）
    note        TEXT
);

CREATE TABLE IF NOT EXISTS documents (
    doc_id        INTEGER PRIMARY KEY,
    code          TEXT NOT NULL REFERENCES companies(code),
    fiscal_year   INTEGER NOT NULL,             -- 文档财年（招股书归最近完整财年）
    doc_type      TEXT NOT NULL DEFAULT 'annual_report',  -- annual_report|prospectus
    title         TEXT NOT NULL,
    ann_time      TEXT,                         -- ISO 日期
    adjunct_url   TEXT NOT NULL UNIQUE,         -- cninfo 附件路径（去重键）
    final_url     TEXT,                         -- https://static.cninfo.com.cn/...
    file_path     TEXT NOT NULL,                -- 相对项目根
    file_size     INTEGER,
    sha256        TEXT NOT NULL,
    page_count    INTEGER,                      -- 文本化后回填
    is_primary    INTEGER NOT NULL DEFAULT 0,   -- 同 (code,fiscal_year) 多候选时的首选
    download_status TEXT NOT NULL DEFAULT 'ok',
    download_error TEXT,
    extracted_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_doc_code_year ON documents(code, fiscal_year);

CREATE TABLE IF NOT EXISTS pages (
    page_id     INTEGER PRIMARY KEY,
    doc_id      INTEGER NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    page_no     INTEGER NOT NULL,               -- 1 起始文档页序（与 CFQA 答案出自对齐）
    text        TEXT NOT NULL,
    tables_json TEXT,                           -- find_tables 输出 JSON
    n_chars     INTEGER,
    UNIQUE (doc_id, page_no)
);

CREATE TABLE IF NOT EXISTS metric_defs (
    key       TEXT PRIMARY KEY,                 -- revenue/net_profit_parent/roe_weighted/...
    cn_name   TEXT NOT NULL,
    category  TEXT NOT NULL,                    -- profitability|solvency|cashflow|per_share|growth
    unit_type TEXT NOT NULL                     -- currency|percent|per_share
);

CREATE TABLE IF NOT EXISTS metrics (
    metric_id       INTEGER PRIMARY KEY,
    code            TEXT NOT NULL REFERENCES companies(code),
    fiscal_year     INTEGER NOT NULL,
    metric_key      TEXT NOT NULL REFERENCES metric_defs(key),
    value           REAL,                       -- 归一化：元 / 百分点 / 元每股
    raw_value       TEXT NOT NULL,
    raw_unit        TEXT,                       -- 元|万元|千元|%|元/股
    source          TEXT NOT NULL,              -- section2|income_stmt|balance_sheet|cashflow|prospectus|baostock|akshare_balance|akshare_income|akshare_cashflow|datasinking
    doc_id          INTEGER REFERENCES documents(doc_id),  -- API 来源为 NULL
    page_no         INTEGER,                    -- API 来源为 NULL
    source_text     TEXT NOT NULL,              -- 原文片段或 API 出处（出处信息链）
    column_label    TEXT NOT NULL,              -- 本期|上年同期|20XX年度
    method          TEXT NOT NULL DEFAULT 'table',  -- table|regex_fallback|api
    confidence      TEXT NOT NULL DEFAULT 'high',   -- high|medium|low
    api_ref         TEXT,                       -- API 出处：baostock / akshare_em / datasinking:<symbol>
    basis           TEXT NOT NULL DEFAULT 'as_reported',  -- 口径：as_reported(年报原文披露值) | restated(API追溯重述值)
    evidence_doc_id INTEGER REFERENCES documents(doc_id),  -- 出处回填：佐证该值的年报文档
    evidence_page_no INTEGER,                    -- 出处回填：佐证该值的年报页码
    cross_metric_id INTEGER REFERENCES metrics(metric_id),
    cross_status    TEXT,                       -- match|mismatch|none
    extracted_at    TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE (doc_id, fiscal_year, metric_key, source, column_label)
);
-- API 来源行（doc_id IS NULL）按 (code, 财年, 指标, 来源, 列) 去重
CREATE UNIQUE INDEX IF NOT EXISTS idx_metrics_api_unique
    ON metrics(code, fiscal_year, metric_key, source, column_label) WHERE doc_id IS NULL;
CREATE INDEX IF NOT EXISTS idx_metrics_lookup ON metrics(code, fiscal_year, metric_key);

CREATE TABLE IF NOT EXISTS report_markdown (
    id          INTEGER PRIMARY KEY,
    code        TEXT NOT NULL REFERENCES companies(code),
    symbol      TEXT NOT NULL,                  -- FMP 风格 600249.SS
    report_period TEXT NOT NULL,                -- 2022-12-31
    doc_type    TEXT NOT NULL DEFAULT 'annual_report',
    title       TEXT,
    adjunct_url TEXT,                           -- 关联 documents.adjunct_url
    content     TEXT,                           -- 全文 Markdown
    source_api  TEXT NOT NULL,                  -- datasinking
    fetched_at  TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE (code, report_period, doc_type)
);

CREATE TABLE IF NOT EXISTS cfqa_questions (
    question_id  INTEGER PRIMARY KEY,           -- CFQA 原始 id
    code         TEXT NOT NULL REFERENCES companies(code),
    company_name TEXT NOT NULL,                 -- 原始公司名（核对用）
    split        TEXT NOT NULL,
    question     TEXT NOT NULL,
    answer       TEXT NOT NULL,
    answer_pages TEXT NOT NULL,                 -- 原始 JSON：[[6],[154]]
    q_year       INTEGER,                       -- 问题中解析的年份
    name_ok      INTEGER NOT NULL DEFAULT 1,    -- 问题标注的公司名与代码所属公司一致（0=数据集噪声）
    qa_doc_map   TEXT,                          -- docmap 结果 JSON
    qa_check     TEXT                           -- 质检②结果 ok|fail|partial|skip
);
CREATE INDEX IF NOT EXISTS idx_cfqa_code ON cfqa_questions(code);

CREATE TABLE IF NOT EXISTS download_log (
    id INTEGER PRIMARY KEY,
    ts TEXT DEFAULT (datetime('now','localtime')),
    code TEXT,
    fiscal_year INTEGER,
    event TEXT NOT NULL,                        -- search|filter|rank|download_ok|retry|missing|fallback
    detail TEXT
);

CREATE TABLE IF NOT EXISTS qc_runs (
    run_id INTEGER PRIMARY KEY,
    ts TEXT DEFAULT (datetime('now','localtime')),
    gate TEXT NOT NULL, pass INTEGER NOT NULL,  -- 1/0
    detail_json TEXT                            -- 失败明细（供报告渲染）
);
