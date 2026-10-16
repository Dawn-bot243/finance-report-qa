"""巨潮资讯网客户端：orgId 查询（代码+名称双核对）、公告查询、PDF 流式下载。"""
from __future__ import annotations

import hashlib
import time
from pathlib import Path

import requests

SEARCH_URL = "http://www.cninfo.com.cn/new/information/topSearch/query"
ANNOUNCE_URL = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
PDF_BASE = "https://static.cninfo.com.cn/"


class CninfoClient:
    def __init__(self, user_agent: str, delay: float = 1.5, retries: int = 3, timeout: int = 60):
        self.ua = user_agent
        self.delay = delay
        self.retries = retries
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent,
            "Referer": "http://www.cninfo.com.cn/new/commonUrl/pageOfSearch?url=disclosure/list/search",
        })
        self._last_req = 0.0

    def _throttle(self) -> None:
        wait = self._last_req + self.delay - time.time()
        if wait > 0:
            time.sleep(wait)
        self._last_req = time.time()

    def _post(self, url: str, data: dict) -> dict:
        self._throttle()
        last_err: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                resp = self.session.post(url, data=data, timeout=self.timeout)
                resp.raise_for_status()
                return resp.json()
            except Exception as e:  # 网络/JSON 异常统一重试
                last_err = e
                time.sleep(2 ** attempt)
        raise RuntimeError(f"请求失败 {url}: {last_err}")

    def search_org(self, keyword: str) -> list[dict]:
        """topSearch → [{code, orgId, zwjc, category, ...}]"""
        data = self._post(SEARCH_URL, {"keyWord": keyword, "maxNum": "10"})
        return data if isinstance(data, list) else []

    def find_org(self, code: str, expect_name: str) -> dict:
        """按代码+公司名双核对找 orgId（防同名异码/同码异名）。"""
        cands = self.search_org(expect_name)
        for c in cands:
            if c.get("code") == code and c.get("zwjc") == expect_name:
                return c
        # 次选：代码匹配但名称不完全一致
        for c in cands:
            if c.get("code") == code:
                return c
        raise RuntimeError(f"topSearch 未找到 {code} {expect_name}，返回 {cands}")

    def query_announcements(
        self,
        stock: str,
        org_id: str,
        *,
        column: str = "sse",
        category: str = "",
        searchkey: str = "",
        se_date: str,
        page: int = 1,
        page_size: int = 30,
    ) -> list[dict]:
        """hisAnnouncement 查询 → [{title, adjunctUrl, announcementTime(毫秒)}]"""
        data = {
            "pageNum": str(page),
            "pageSize": str(page_size),
            "column": column,
            "tabName": "fulltext",
            "plate": "",
            "stock": f"{stock},{org_id}",
            "searchkey": searchkey,
            "secid": "",
            "category": category,
            "trade": "",
            "seDate": se_date,
            "sortName": "",
            "sortType": "",
            "isHLtitle": "true",
        }
        d = self._post(ANNOUNCE_URL, data)
        anns = d.get("announcements") or []
        out = []
        for a in anns:
            title = a.get("announcementTitle", "")
            title = title.replace("<em>", "").replace("</em>", "")
            out.append({
                "title": title,
                "adjunctUrl": a.get("adjunctUrl", ""),
                "announcementTime": a.get("announcementTime", 0),
            })
        return out

    def download_pdf(self, adjunct_url: str, dest: Path) -> dict:
        """下载 PDF 到临时文件后原子改名。返回 {sha256, size}。"""
        url = PDF_BASE + adjunct_url
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        last_err: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                with self.session.get(url, stream=True, timeout=self.timeout) as r:
                    r.raise_for_status()
                    sha = hashlib.sha256()
                    size = 0
                    with open(tmp, "wb") as f:
                        for chunk in r.iter_content(chunk_size=1 << 16):
                            f.write(chunk)
                            sha.update(chunk)
                            size += len(chunk)
                if not tmp.read_bytes().startswith(b"%PDF"):
                    raise RuntimeError(f"非 PDF 内容: {url}")
                tmp.replace(dest)
                return {"sha256": sha.hexdigest(), "size": size}
            except Exception as e:
                last_err = e
                time.sleep(2 ** attempt)
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise RuntimeError(f"下载失败 {url}: {last_err}")
