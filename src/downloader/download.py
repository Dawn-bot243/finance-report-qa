"""下载编排：公司×年份 → 候选公告 → 过滤/排名 → 招股书回退 → 下载落库（幂等）。"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .. import paths
from ..config import Company, Config
from ..logutil import setup_logging
from ..store import db
from .cninfo import CninfoClient

log = setup_logging(__name__)

_SUMMARY_PAT = re.compile(r"摘要|英文")
_CORRECT_PAT = re.compile(r"更正|修订|更新")
_YEAR_TITLE_PAT = re.compile(r"(20\d{2})\s*年.*年度报告")


@dataclass
class Announcement:
    title: str
    adjunct_url: str
    ann_time: int  # 毫秒时间戳
    fiscal_year: int | None
    is_summary: bool
    is_corrected: bool

    @property
    def ann_date(self) -> str:
        return datetime.fromtimestamp(self.ann_time / 1000).strftime("%Y-%m-%d")


def parse_candidates(cands: list[dict]) -> list[Announcement]:
    out = []
    for c in cands:
        title = c["title"]
        m = _YEAR_TITLE_PAT.search(title)
        fy = int(m.group(1)) if m else None
        out.append(Announcement(
            title=title,
            adjunct_url=c["adjunctUrl"],
            ann_time=c["announcementTime"],
            fiscal_year=fy,
            is_summary=bool(_SUMMARY_PAT.search(title)),
            is_corrected=bool(_CORRECT_PAT.search(title)),
        ))
    return out


def rank_pick(cands: list[Announcement], fiscal_year: int) -> list[Announcement]:
    """返回按优先级排序的候选（最多前2个），供 docmap 定主。"""
    kept = [a for a in cands if not a.is_summary and a.fiscal_year == fiscal_year]
    # 更正在前；同级取披露时间最新
    kept.sort(key=lambda a: (not a.is_corrected, -a.ann_time))
    return kept[:2]


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _insert_document(conn: sqlite3.Connection, row: dict) -> int:
    """按 adjunct_url 幂等 upsert documents，返回 doc_id。"""
    cur = conn.execute("SELECT doc_id FROM documents WHERE adjunct_url=?", (row["adjunct_url"],))
    r = cur.fetchone()
    if r:
        return r["doc_id"]
    cur = conn.execute(
        """INSERT INTO documents (code, fiscal_year, doc_type, title, ann_time, adjunct_url,
                                 final_url, file_path, file_size, sha256, is_primary)
           VALUES (:code,:fiscal_year,:doc_type,:title,:ann_time,:adjunct_url,
                   :final_url,:file_path,:file_size,:sha256,:is_primary)""",
        row,
    )
    return cur.lastrowid


def _log(conn: sqlite3.Connection, code: str | None, fy: int | None, event: str, detail: str) -> None:
    conn.execute("INSERT INTO download_log (code, fiscal_year, event, detail) VALUES (?,?,?,?)",
                 (code, fy, event, detail))


def _download_one(
    conn: sqlite3.Connection,
    client: CninfoClient,
    company: Company,
    ann: Announcement,
    doc_type: str,
    rank: int,
    force: bool,
) -> int | None:
    """下载单个公告 PDF 并落库；已存在且文件完好则跳过。返回 doc_id。"""
    dest = paths.dir_pdf() / company.code / (
        f"{company.code}_{ann.fiscal_year}_{doc_type}_{ann.ann_date}_{ann.adjunct_url.rsplit('/',1)[-1]}"
    )
    if not force and dest.exists():
        size = dest.stat().st_size
        try:
            sha = _sha256_file(dest)
        except OSError:
            sha = ""
        row = {
            "code": company.code, "fiscal_year": ann.fiscal_year, "doc_type": doc_type,
            "title": ann.title, "ann_time": ann.ann_date, "adjunct_url": ann.adjunct_url,
            "final_url": "https://static.cninfo.com.cn/" + ann.adjunct_url,
            "file_path": str(dest.relative_to(paths.PROJECT_ROOT)),
            "file_size": size, "sha256": sha, "is_primary": 1 if rank == 0 else 0,
        }
        doc_id = _insert_document(conn, row)
        _log(conn, company.code, ann.fiscal_year, "download_ok", f"skip existing: {dest.name}")
        return doc_id

    try:
        info = client.download_pdf(ann.adjunct_url, dest)
    except Exception as e:
        _log(conn, company.code, ann.fiscal_year, "retry", f"{ann.title}: {e}")
        raise
    row = {
        "code": company.code, "fiscal_year": ann.fiscal_year, "doc_type": doc_type,
        "title": ann.title, "ann_time": ann.ann_date, "adjunct_url": ann.adjunct_url,
        "final_url": "https://static.cninfo.com.cn/" + ann.adjunct_url,
        "file_path": str(dest.relative_to(paths.PROJECT_ROOT)),
        "file_size": info["size"], "sha256": info["sha256"], "is_primary": 1 if rank == 0 else 0,
    }
    doc_id = _insert_document(conn, row)
    _log(conn, company.code, ann.fiscal_year, "download_ok",
         f"{ann.title} {info['size']}B sha256={info['sha256'][:12]}")
    return doc_id


def download_documents(cfg: Config, force: bool = False, only: str | None = None) -> dict:
    """全量下载。返回统计 {downloaded, skipped, missing}。"""
    paths.ensure_dirs()
    conn = db.connect()
    db.init_schema(conn)
    dcfg = cfg.download
    client = CninfoClient(
        user_agent=dcfg.get("user_agent", "Mozilla/5.0"),
        delay=float(dcfg.get("delay_seconds", 1.5)),
        retries=int(dcfg.get("retries", 3)),
    )
    stats = {"downloaded": 0, "skipped": 0, "missing": []}

    for company in cfg.companies:
        if only and company.code != only:
            continue
        log.info("== %s %s", company.code, company.name)
        org = client.find_org(company.code, company.name)
        log.info("orgId=%s (%s)", org.get("orgId"), org.get("category"))
        db.upsert(conn, "companies", {
            "code": company.code, "name": company.name, "org_id": org.get("orgId"),
            "market": company.market, "listed_date": company.listed_date, "note": company.note,
        }, ("code",))

        for fy in cfg.fiscal_years:
            is_pre_listing = company.listed_date and int(company.listed_date[:4]) > fy
            if is_pre_listing:
                # 上市前年份：走招股书回退（config.prospectus_fallback 指定）
                fallback = cfg.prospectus_fallback.get(company.code, [])
                if not fallback:
                    _log(conn, company.code, fy, "missing", "上市前无年报且无招股书候选")
                    stats["missing"].append(f"{company.code} {fy}")
                    continue
                log.info("FY%s 上市前，招股书回退 %d 个候选", fy, len(fallback))
                for rank, adjunct in enumerate(fallback):
                    # 招股书标题从 cninfo 反查
                    try:
                        anns = client.query_announcements(
                            company.code, org.get("orgId"), searchkey="招股说明书",
                            se_date=f"{fy}-01-01~{fy + 2}-06-30")
                        meta = next((a for a in anns if a["adjunctUrl"] == adjunct), None)
                    except Exception:
                        meta = None
                    ann = Announcement(
                        title=(meta or {}).get("title", f"{company.name}招股说明书"),
                        adjunct_url=adjunct,
                        ann_time=(meta or {}).get("announcementTime", 0),
                        fiscal_year=fy,
                        is_summary=False, is_corrected=False,
                    )
                    try:
                        _download_one(conn, client, company, ann, "prospectus", rank, force)
                        stats["downloaded"] += 1
                        _log(conn, company.code, fy, "fallback", f"招股书: {adjunct}")
                    except Exception as e:
                        _log(conn, company.code, fy, "missing", f"招股书下载失败: {e}")
                        stats["missing"].append(f"{company.code} {fy} prospectus {adjunct}")
                continue

            # 正常年报：FY 年报告披露于 FY+1 上半年（或 FY 年末）
            try:
                raw = client.query_announcements(
                    company.code, org.get("orgId"),
                    column="sse" if company.market == "sse" else "szse",
                    category=dcfg.get("annual_report_category", "category_ndbg_szsh;"),
                    se_date=f"{fy}-01-01~{fy + 1}-06-30",
                    page_size=50,
                )
            except Exception as e:
                _log(conn, company.code, fy, "retry", f"公告查询失败: {e}")
                stats["missing"].append(f"{company.code} {fy} query failed")
                continue
            cands = parse_candidates(raw)
            picks = rank_pick(cands, fy)
            _log(conn, company.code, fy, "search",
                 f"total={len(raw)} candidates=" + ";".join(a.title for a in picks[:2]))
            if not picks:
                _log(conn, company.code, fy, "missing", "无合格年报候选")
                stats["missing"].append(f"{company.code} {fy}")
                continue
            for rank, ann in enumerate(picks):
                try:
                    _download_one(conn, client, company, ann, "annual_report", rank, force)
                    stats["downloaded"] += 1
                except Exception as e:
                    _log(conn, company.code, fy, "retry", f"下载失败: {e}")
                    stats["missing"].append(f"{company.code} {fy} {ann.adjunct_url}")

    conn.commit()
    conn.close()
    return stats


def list_missing(cfg: Config) -> list[str]:
    """打印并返回当前缺失的 (code, year) 组合。"""
    conn = db.connect()
    rows = conn.execute(
        "SELECT code, fiscal_year, doc_type, is_primary FROM documents ORDER BY code, fiscal_year"
    ).fetchall()
    have = {(r["code"], r["fiscal_year"], r["doc_type"]) for r in rows if r["is_primary"]}
    missing = []
    for c in cfg.companies:
        for fy in cfg.fiscal_years:
            if (c.code, fy, "annual_report") not in have and (c.code, fy, "prospectus") not in have:
                missing.append(f"{c.code} {fy}")
    conn.close()
    return missing
