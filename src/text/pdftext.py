"""PDF 文本化：pymupdf 逐页提取文本+表格，页眉页脚清洗，入库 pages 表。"""
from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pymupdf as fitz

from .. import paths
from ..config import Config
from ..logutil import setup_logging
from ..store import db

log = setup_logging(__name__)

_PAGE_NUM_LINE = re.compile(r"^\s*\d{1,4}\s*(/\s*\d{1,4})?\s*$")


@dataclass
class PageText:
    page_no: int       # 1 起始文档页序（与 CFQA 答案出自对齐）
    text: str          # 清洗后正文（含表格渲染行）
    tables_json: str   # find_tables 输出 JSON
    n_chars: int


def _blocks_to_text(page: fitz.Page) -> str:
    """按 (y, x) 排序的 blocks 拼文本，处理多栏页。"""
    blocks = page.get_text("blocks")
    blocks.sort(key=lambda b: (round(b[1], 1), b[0]))
    lines = []
    for b in blocks:
        t = (b[4] or "").strip()
        if t:
            lines.append(t)
    return "\n".join(lines)


def _tables_to_lines(tables) -> str:
    """表格渲染行：每行单元格以 | 连接（含表头）。"""
    out = []
    for tab in tables:
        for row in tab.extract():
            cells = [str(c or "").replace("\n", " ").strip() for c in row]
            out.append(" | ".join(cells))
    return "\n".join(out)


def extract_document(pdf_path: Path, header_repeat_threshold: float = 0.6) -> tuple[list[PageText], int]:
    """提取单份 PDF。页眉页脚按整份文档的重复频率清洗。返回 (页列表, 总页数)。"""
    doc = fitz.open(pdf_path)
    raw_pages: list[tuple[int, list[str], str, list]] = []  # (page_no, body_lines, table_lines, tables)
    for i, page in enumerate(doc):
        body = _blocks_to_text(page)
        try:
            page_tables = page.find_tables().tables
        except Exception:
            page_tables = []
        tbl = _tables_to_lines(page_tables)
        raw_pages.append((i + 1, body.split("\n"), tbl, page_tables))
    n = len(raw_pages)
    # 跨文档统计：≥60% 页面出现且 <60 字符的完全重复行 → 页眉页脚
    all_counts = Counter(ln for _, lines, _, _ in raw_pages for ln in lines)
    pages: list[PageText] = []
    for page_no, lines, tbl, page_tables in raw_pages:
        kept = [
            ln for ln in lines
            if not _PAGE_NUM_LINE.match(ln)
            and not (all_counts[ln] >= n * header_repeat_threshold and len(ln) < 60)
        ]
        text = "\n".join(kept + (tbl.split("\n") if tbl else []))
        tables_json = json.dumps(
            [{"rows": t.extract(), "bbox": list(t.bbox)} for t in page_tables],
            ensure_ascii=False, default=str,
        )
        pages.append(PageText(
            page_no=page_no,
            text=text,
            tables_json=tables_json,
            n_chars=len(text),
        ))
    doc.close()
    return pages, len(pages)


def _insert_pages(conn: sqlite3.Connection, doc_id: int, pages: list[PageText]) -> None:
    conn.execute("DELETE FROM pages WHERE doc_id=?", (doc_id,))
    conn.executemany(
        "INSERT INTO pages (doc_id, page_no, text, tables_json, n_chars) VALUES (?,?,?,?,?)",
        [(doc_id, p.page_no, p.text, p.tables_json, p.n_chars) for p in pages],
    )
    conn.execute("UPDATE documents SET page_count=?, extracted_at=datetime('now','localtime') WHERE doc_id=?",
                 (len(pages), doc_id))


def text_all_documents(cfg: Config, force: bool = False, only: str | None = None) -> dict:
    """全量文本化。返回统计。"""
    conn = db.connect()
    stats = {"done": 0, "skipped": 0, "failed": []}
    rows = conn.execute(
        "SELECT doc_id, code, file_path, page_count FROM documents ORDER BY doc_id"
    ).fetchall()
    for r in rows:
        if only and r["code"] != only:
            continue
        if not force and r["page_count"]:
            stats["skipped"] += 1
            continue
        pdf_path = paths.PROJECT_ROOT / r["file_path"]
        if not pdf_path.exists():
            stats["failed"].append(f"doc {r['doc_id']} 文件缺失: {pdf_path}")
            continue
        try:
            pages, n = extract_document(pdf_path)
            _insert_pages(conn, r["doc_id"], pages)
            log.info("doc %d: %d 页, 共 %d 字符", r["doc_id"], n, sum(p.n_chars for p in pages))
            stats["done"] += 1
        except Exception as e:
            log.exception("doc %d 文本化失败", r["doc_id"])
            stats["failed"].append(f"doc {r['doc_id']}: {e}")
    conn.commit()
    conn.close()
    log.info("文本化完成: %s", stats)
    return stats
