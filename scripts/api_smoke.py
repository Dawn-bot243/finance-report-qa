#!/usr/bin/env python
"""API 冒烟与延迟测试（进程内 TestClient，无需先启动服务）。

用法：
    python -X utf8 scripts/api_smoke.py
"""
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from src.api.server import app

client = TestClient(app)

CASES = [
    ("GET", "/health", None),
    ("GET", "/companies", None),
    ("GET", "/metric_defs", None),
    ("GET", "/metrics", {"code": "600249", "year": 2022}),
    ("GET", "/metrics", {"code": "688608", "year": 2020}),
    ("GET", "/compare", {"codes": "600249,688366", "year": 2022}),
    ("GET", "/compare", {"codes": "688608,688595", "year": 2019}),
    ("GET", "/kb", {"question": "ROE是什么意思"}),
    ("GET", "/kb", {"question": "毛利率是什么"}),
    ("POST", "/ask", {"question": "两面针2019年营业收入是多少"}),
    ("POST", "/ask", {"question": "毛利率是什么"}),
    ("POST", "/ask", {"question": "恒玄科技2020年研发投入情况"}),
    ("POST", "/ask", {"question": "昊海生科2021年有哪些品牌中标"}),
    ("POST", "/ask", {"question": "建龙微纳2022年成型分子筛产品的营业收入"}),
]


def run_once(method: str, path: str, payload: dict | None) -> tuple[float, int, dict | str]:
    t0 = time.perf_counter()
    if method == "GET":
        r = client.get(path, params=payload)
    else:
        r = client.post(path, json=payload)
    dt = time.perf_counter() - t0
    try:
        body = r.json()
    except Exception:
        body = r.text
    return dt, r.status_code, body


def main() -> int:
    rows = []
    failures = 0
    # 预热（BM25/向量首次加载）
    run_once("GET", "/health", None)
    run_once("POST", "/ask", {"question": "两面针2019年营业收入是多少"})

    for method, path, payload in CASES:
        dt, status, body = run_once(method, path, payload)
        ok = 200 <= status < 300
        failures += 0 if ok else 1
        detail = ""
        if isinstance(body, dict):
            if path == "/ask":
                detail = f"path={body.get('path')}"
            elif path == "/kb":
                detail = f"matched={body.get('matched')}"
            elif path == "/compare":
                detail = f"{body.get('comparable_rows')}/{body.get('total_rows')} 可比"
        rows.append((method, path, status, round(dt, 3), detail))

    print(f"{'方法':6s} {'路径':12s} {'状态':4s} {'耗时(s)':8s} 说明")
    for method, path, status, dt, detail in rows:
        print(f"{method:6s} {path:12s} {status:<4d} {dt:<8.3f} {detail}")
    lat = sorted(r[3] for r in rows)
    p95 = lat[min(len(lat) - 1, int(len(lat) * 0.95))]
    print(f"\n请求 {len(rows)} 个 · 失败 {failures} 个 · "
          f"平均 {statistics.mean(lat):.3f}s · P95 {p95:.3f}s")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
