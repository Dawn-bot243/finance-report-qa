"""质检报告：reports/qc_report_<时间戳>.md + 提取率矩阵 CSV。"""
from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from .. import paths


def write_report(conn: sqlite3.Connection, results: dict[str, tuple[bool, dict]]) -> Path:
    paths.ensure_dirs()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    report = paths.dir_reports() / f"qc_report_{ts}.md"
    lines = [f"# 质检报告 {datetime.now().strftime('%Y-%m-%d %H:%M')}", ""]

    for gate in ("gate1", "gate2", "gate3", "gate4", "compare"):
        passed, d = results.get(gate, (False, {}))
        lines.append(f"## {gate}: {'✅ PASS' if passed else '❌ FAIL'}")
        if gate == "gate1":
            lines.append(f"- 问题数: {len(d.get('problems', []))}")
            for p in d.get("problems", [])[:30]:
                lines.append(f"  - {p}")
        elif gate == "gate2":
            lines.append(f"- 总体命中率: **{d.get('overall_hit_rate')}** (目标 ≥{d.get('target')})")
            lines.append(f"- 有效 {d.get('total_valid')} 条 / 命中 {d.get('total_hit')} 条")
            lines.append(f"- 排除项：空页码 {d.get('skip_empty_pages')} 条 · 公司名错配 {d.get('dataset_name_mismatch')} 条 · "
                         f"未披露类答案 {d.get('unverifiable_negative')} 条 · 数字不在文档（版本差异）{d.get('unverifiable_version')} 条")
            lines.append("- 按公司×年份:")
            for k, v in d.get("per_company_year", {}).items():
                lines.append(f"  - {k}: {v}")
        elif gate == "gate3":
            lines.append(f"- 校验 {d.get('checked')} 对 / mismatch {d.get('mismatch_count')} 对 ({d.get('mismatch_rate')})")
            for m in d.get("mismatches", [])[:30]:
                lines.append(f"  - {m['code']} FY{m['fiscal_year']} {m['metric_key']} [{m['source']}]={m['value']} vs 其他源 (err)")
        elif gate == "gate4":
            lines.append(f"- 第二节核心指标提取率: **{d.get('s2_rate')}** (目标 ≥{d.get('s2_target')})")
            lines.append(f"- 三大报表核心行提取率: **{d.get('stmt_rate')}** (目标 ≥{d.get('stmt_target')})")
            lines.append(f"- 算法漏提 {d.get('missing_count')} 项 / 确实未披露 {d.get('undisclosed_count')} 项（后者不计入分母）:")
            for m in d.get("missing", [])[:50]:
                lines.append(f"  - {m}")
        elif gate == "compare":
            lines.append(f"- 覆盖单元总数（20 个核心指标 × 5 公司 × 4 财年）: {d.get('total_cells')}")
            lines.append(f"- PDF 提取覆盖率: **{d.get('pdf_coverage_rate')}** vs API 覆盖率: **{d.get('api_coverage_rate')}**")
            lines.append(f"- 重叠指标一致性: 一致 {d.get('pdf_api_agree')} / 不一致 {d.get('pdf_api_disagree')} "
                         f"(一致率 {d.get('pdf_api_agreement_rate')})")
            lines.append(f"- 仅PDF有值 {d.get('pdf_only_count')} 项 / 仅API有值 {d.get('api_only_count')} 项")
            lines.append(f"- baostock vs akshare 互验: 一致 {d.get('baostock_vs_akshare_agree')} / "
                         f"不一致 {d.get('baostock_vs_akshare_disagree')} "
                         f"(一致率 {d.get('baostock_vs_akshare_rate')})")
            lines.append(f"- datasinking vs PDF 同源校验: 一致 {d.get('datasinking_vs_pdf_agree')} / "
                         f"不一致 {d.get('datasinking_vs_pdf_disagree')} "
                         f"(一致率 {d.get('datasinking_vs_pdf_rate')})")
            lines.append(f"- 口径分布: as_reported {d.get('basis_as_reported_rows')} 行 / "
                         f"restated {d.get('basis_restated_rows')} 行")
            lines.append(f"- 出处回填: API 行共 {d.get('api_rows_total')}，已回填年报出处 {d.get('evidence_backfilled')} "
                         f"({d.get('evidence_backfill_rate')})，数值不符未回填 {d.get('evidence_mismatch')}")
            if d.get("disagreements"):
                lines.append("- PDF 与 API 不一致明细（code FY metric | pdf值 | api值）:")
                for code, fy, key, pv, av, err in d["disagreements"][:20]:
                    lines.append(f"  - {code} FY{fy} {key}: pdf={pv} api={av} (err={err:.2e})")
        lines.append("")

    report.write_text("\n".join(lines), encoding="utf-8")

    # 提取率矩阵 CSV
    g4 = results.get("gate4", (False, {}))[1]
    matrix_path = paths.dir_reports() / f"extraction_matrix_{ts}.csv"
    with open(matrix_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["code", "fiscal_year", "metric_key", "extracted"])
        for k, v in sorted(g4.get("matrix", {}).items()):
            code, year, key = k.split("|", 2)
            w.writerow([code, year, key, 1 if v else 0])
    return report
