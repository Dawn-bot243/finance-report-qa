"""SQLite 连接与初始化：WAL、外键 ON、幂等 upsert 辅助。"""
import sqlite3
from pathlib import Path

from .. import paths

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def connect(db: Path | None = None) -> sqlite3.Connection:
    p = db or paths.db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    with open(_SCHEMA_PATH, encoding="utf-8") as f:
        conn.executescript(f.read())
    conn.commit()


def upsert(conn: sqlite3.Connection, table: str, row: dict, conflict_cols: tuple[str, ...]) -> None:
    """幂等 upsert：冲突列冲突时更新其余字段。"""
    cols = list(row.keys())
    placeholders = ",".join("?" for _ in cols)
    updates = ",".join(f"{c}=excluded.{c}" for c in cols if c not in conflict_cols)
    on_conflict = ",".join(conflict_cols)
    sql = (f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders}) "
           f"ON CONFLICT ({on_conflict}) DO UPDATE SET {updates}")
    conn.execute(sql, [row[c] for c in cols])
