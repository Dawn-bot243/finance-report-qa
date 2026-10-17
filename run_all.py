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
    """RAG 子命令：build|ask|eval（args.rag_cmd + args.question）"""
    from src.rag.indexer import build_index
    from src.rag.ask import ask as rag_ask, print_answer
    from src.rag.eval import run_eval
    if args.rag_cmd == "build":
        build_index(cfg, force=args.force)
    elif args.rag_cmd == "ask":
        if not args.question:
            raise SystemExit("ask 需要 --question")
        print_answer(rag_ask(args.question, cfg, verbose=args.verbose))
    elif args.rag_cmd == "eval":
        run_eval(cfg, limit=args.eval_limit, skip_judge=args.skip_judge)


def main():
    parser = argparse.ArgumentParser(description="财报库阶段一流水线")
    parser.add_argument("step", choices=["init", "cfqa", "download", "text", "apis", "metrics", "qc",
                                         "rag", "all"])
    parser.add_argument("--force", action="store_true", help="强制重跑（覆盖已有结果）")
    parser.add_argument("--only", help="只处理指定股票代码")
    parser.add_argument("--list-missing", action="store_true", help="[download] 列出缺失文档")
    parser.add_argument("--qc-sample", type=int, default=20, help="[qc] Gate2 人工抽查条数")
    parser.add_argument("rag_cmd", nargs="?", choices=["build", "ask", "eval"],
                        help="[rag] 子命令")
    parser.add_argument("--question", help="[rag ask] 问题文本")
    parser.add_argument("--verbose", action="store_true", help="[rag ask] 打印检索详情")
    parser.add_argument("--eval-limit", type=int, help="[rag eval] 只评估前 N 题")
    parser.add_argument("--skip-judge", action="store_true", help="[rag eval] 跳过 LLM 判分")
    args = parser.parse_args()

    paths.ensure_dirs()
    cfg = load()
    steps = ["init", "cfqa", "download", "text", "apis", "metrics", "qc"] if args.step == "all" else [args.step]
    for s in steps:
        log.info(">>> 步骤 %s 开始", s)
        {"init": cmd_init, "cfqa": cmd_cfqa, "download": cmd_download,
         "text": cmd_text, "apis": cmd_apis, "metrics": cmd_metrics,
         "qc": cmd_qc, "rag": cmd_rag}[s](cfg, args)
        log.info("<<< 步骤 %s 结束", s)
    return 0


if __name__ == "__main__":
    sys.exit(main())
