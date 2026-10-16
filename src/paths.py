"""项目路径定位：用 __file__ 上溯定位项目根，不依赖 cwd（避免 shell 间中文路径问题）。"""
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def project_root() -> Path:
    return PROJECT_ROOT


def dir_raw() -> Path:
    return PROJECT_ROOT / "raw"


def dir_pdf() -> Path:
    return PROJECT_ROOT / "raw" / "pdf"


def dir_db() -> Path:
    return PROJECT_ROOT / "db"


def dir_reports() -> Path:
    return PROJECT_ROOT / "reports"


def dir_logs() -> Path:
    return PROJECT_ROOT / "logs"


def db_path() -> Path:
    return dir_db() / "finance.db"


def ensure_dirs() -> None:
    for d in (dir_pdf(), dir_db(), dir_reports(), dir_logs()):
        d.mkdir(parents=True, exist_ok=True)
