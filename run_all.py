#!/usr/bin/env python
"""阶段一单入口 CLI（幂等，--force 重跑）。

用法（Windows + Git Bash 统一用 -X utf8）：
    python -X utf8 run_all.py init                 # 初始化 schema + metric_defs
    python -X utf8 run_all.py cfqa                 # 导入 CFQA 问答
    python -X utf8 run_all.py download             # 下载年报/招股书 PDF
    python -X utf8 run_all.py text                 # PDF 文本化入库
    python -X utf8 run_all.py metrics              # 结构化指标提取 + 交叉校验
    python -X utf8 run_all.py qc                   # 四道质量门 + 报告
    python -X utf8 run_all.py all                  # 顺序执行全部
    python -X utf8 run_all.py download --only 600249
"""
import argparse
import sys

from src import paths
from src.config import load
from src.logutil import setup_logging

log = setup_logging("run_all")


def cmd_init(cfg, args):
    from src.store import db
    conn = db.connect()
    db.init_schema(conn)
    from src.metrics.defs import METRIC_DEFS
    for m in METRIC_DEFS:
        db.upsert(conn, "metric_defs", {
            "key": m.key, "cn_name": m.cn_name, "category": m.category, "unit_type": m.unit_type,
        }, ("key",))
    conn.commit()
    conn.close()
    log.info("init 完成：schema + %d 个指标定义", len(METRIC_DEFS))


def cmd_cfqa(cfg, args):
    from src.cfqa.importer import import_cfqa
    import_cfqa(cfg, force=args.force)


def cmd_download(cfg, args):
    from src.downloader.download import download_documents, list_missing
    if args.list_missing:
        missing = list_missing(cfg)
        print("缺失 (code, year):", missing if missing else "无")
        return
    stats = download_documents(cfg, force=args.force, only=args.only)
    log.info("下载完成: %s", stats)


def cmd_text(cfg, args):
    from src.text.pdftext import text_all_documents
    text_all_documents(cfg, force=args.force, only=args.only)


def cmd_apis(cfg, args):
    from src.apis.importer import import_all_apis
    import_all_apis(cfg, force=args.force, only=args.only)


def cmd_metrics(cfg, args):
    from src.metrics.extract import extract_all
    extract_all(cfg, force=args.force, only=args.only)


def cmd_qc(cfg, args):
    from src.qa.qc import run_all_gates
    run_all_gates(cfg, sample=args.qc_sample)


def cmd_rag(cfg, args):
    """RAG 子命令：build|ask|eval（args.subcmd + args.question）"""
    from src.rag.indexer import build_index
    from src.rag.ask import ask as rag_ask, print_answer
    from src.rag.eval import run_eval
    if args.subcmd == "build":
        build_index(cfg, force=args.force)
    elif args.subcmd == "ask":
        if not args.question:
            raise SystemExit("ask 需要 --question")
        print_answer(rag_ask(args.question, cfg, verbose=args.verbose))
    elif args.subcmd == "eval":
        run_eval(cfg, limit=args.eval_limit, skip_judge=args.skip_judge)
    else:
        raise SystemExit("rag 子命令: build|ask|eval")


def cmd_agent(cfg, args):
    """Agent 子命令：ask|eval|debate（LLM 自主调用工具）"""
    from src.agent.loop import agent_ask, print_agent_answer
    from src.agent.eval import run_agent_eval
    if args.subcmd == "ask":
        if not args.question:
            raise SystemExit("agent ask 需要 --question")
        print_answer_result = agent_ask(args.question, cfg,
                                        max_iters=args.agent_max_iters,
                                        verbose=args.verbose)
        print_agent_answer(print_answer_result)
    elif args.subcmd == "eval":
        run_agent_eval(cfg, limit=args.eval_limit, skip_judge=args.skip_judge,
                       max_iters=args.agent_max_iters)
    elif args.subcmd == "debate":
        from src.agent.debate import debate, print_debate
        if not args.company or not args.year:
            raise SystemExit("agent debate 需要 --company（股票代码）与 --year")
        print_debate(debate(args.company, args.year, cfg, rounds=args.rounds,
                            verbose=args.verbose))
    else:
        raise SystemExit("agent 子命令: ask|eval|debate")


def cmd_serve(cfg, args):
    """启动 FastAPI 后端（uvicorn）。"""
    import uvicorn
    uvicorn.run("src.api.server:app", host=args.host, port=args.port, reload=False)


def cmd_analysis(cfg, args):
    """分析子命令：compare|kb|verify"""
    import sqlite3
    from src import paths
    from src.analysis.compare import compare_peers, render_markdown, write_reports
    from src.analysis.verify import format_findings, verify_numbers
    from src.kb.metric_kb import faq_answer, match_metric, format_entry

    if args.subcmd == "kb":
        if not args.question:
            raise SystemExit("kb 需要 --question")
        e = match_metric(args.question)
        if not e:
            print("知识库未收录该指标")
            return
        print(format_entry(e))
        return

    conn = sqlite3.connect(paths.db_path())
    conn.row_factory = sqlite3.Row
    try:
        if args.subcmd == "compare":
            if not args.codes:
                raise SystemExit("compare 需要 --codes（2-3 个，逗号分隔）")
            codes = [c.strip() for c in args.codes.split(",") if c.strip()]
            if not 2 <= len(codes) <= 3:
                raise SystemExit("同业对比建议选 2-3 家公司")
            if not args.year:
                raise SystemExit("compare 需要 --year（如 2022）")
            metrics = [m.strip() for m in args.metrics.split(",")] if args.metrics else None
            res = compare_peers(conn, codes, args.year, metrics)
            md, csv = write_reports(res)
            print(render_markdown(res))
            print(f"\n报告: {md}\nCSV : {csv}")
        elif args.subcmd == "verify":
            if not args.text:
                raise SystemExit("verify 需要 --text")
            check = verify_numbers(conn, args.text, args.only,
                                   args.year if args.year else None)
            print(f"核验指标陈述 {check['total']} 处：一致 {check['matched']}，"
                  f"待核实 {len(check['suspicious'])}")
            note = format_findings(check)
            if note:
                print(note)
        else:
            raise SystemExit("analysis 子命令: compare|kb|verify")
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="财报库阶段一流水线")
    parser.add_argument("step", choices=["init", "cfqa", "download", "text", "apis", "metrics", "qc",
                                         "rag", "analysis", "agent", "serve", "all"])
    parser.add_argument("subcmd", nargs="?", help="[rag] build|ask|eval；[analysis] compare|kb|verify")
    parser.add_argument("--force", action="store_true", help="强制重跑（覆盖已有结果）")
    parser.add_argument("--only", help="只处理指定股票代码")
    parser.add_argument("--list-missing", action="store_true", help="[download] 列出缺失文档")
    parser.add_argument("--qc-sample", type=int, default=20, help="[qc] Gate2 人工抽查条数")
    parser.add_argument("--question", help="[rag ask / analysis kb] 问题文本")
    parser.add_argument("--verbose", action="store_true", help="[rag ask] 打印检索详情")
    parser.add_argument("--eval-limit", type=int, help="[rag eval] 只评估前 N 题")
    parser.add_argument("--skip-judge", action="store_true", help="[rag eval] 跳过 LLM 判分")
    parser.add_argument("--codes", help="[analysis compare] 股票代码，逗号分隔（2-3 家）")
    parser.add_argument("--year", type=int, help="[analysis compare] 报告期年份")
    parser.add_argument("--metrics", help="[analysis compare] 指标 key，逗号分隔（默认核心15项）")
    parser.add_argument("--text", help="[analysis verify] 待核验文本")
    parser.add_argument("--agent-max-iters", type=int, default=6,
                        help="[agent] 最大工具调用轮数")
    parser.add_argument("--company", help="[agent debate] 股票代码（6 位）")
    parser.add_argument("--rounds", type=int, default=3, help="[agent debate] 辩论轮数（1-5）")
    parser.add_argument("--host", default="127.0.0.1", help="[serve] 监听地址")
    parser.add_argument("--port", type=int, default=8000, help="[serve] 端口")
    args = parser.parse_args()

    paths.ensure_dirs()
    cfg = load()
    steps = ["init", "cfqa", "download", "text", "apis", "metrics", "qc"] if args.step == "all" else [args.step]
    for s in steps:
        log.info(">>> 步骤 %s 开始", s)
        {"init": cmd_init, "cfqa": cmd_cfqa, "download": cmd_download,
         "text": cmd_text, "apis": cmd_apis, "metrics": cmd_metrics,
         "qc": cmd_qc, "rag": cmd_rag, "analysis": cmd_analysis,
         "agent": cmd_agent, "serve": cmd_serve}[s](cfg, args)
        log.info("<<< 步骤 %s 结束", s)
    return 0


if __name__ == "__main__":
    sys.exit(main())
