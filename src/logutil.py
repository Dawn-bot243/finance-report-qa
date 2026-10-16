"""日志：控制台（UTF-8 + 无法编码替换，GBK 控制台安全）+ 文件（UTF-8）。"""
import logging
import sys

from . import paths

_FMT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def setup_logging(name: str = "finance") -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(logging.Formatter(_FMT))
    logger.addHandler(ch)

    paths.ensure_dirs()
    fh = logging.FileHandler(paths.dir_logs() / "run_all.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter(_FMT))
    logger.addHandler(fh)
    return logger
