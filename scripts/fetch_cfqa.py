#!/usr/bin/env python
"""下载 CFQA 数据集切分文件到 raw/cfqa/（仓库不随附原始数据）。

来源：https://github.com/ygan/CFQA（EPJ Data Science 2025）
GitHub 直连不稳时自动回退 jsDelivr CDN 镜像。

用法：python -X utf8 scripts/fetch_cfqa.py
"""
import sys
from pathlib import Path

import requests

BASE_URLS = [
    "https://raw.githubusercontent.com/ygan/CFQA/main/dataset/split_by_company/{}",
    "https://cdn.jsdelivr.net/gh/ygan/CFQA@main/dataset/split_by_company/{}",
]
FILES = [
    "split_by_company_train.json",
    "split_by_company_dev.json",
    "split_by_company_test.json",
]


def main() -> int:
    dest = Path(__file__).resolve().parent.parent / "raw" / "cfqa"
    dest.mkdir(parents=True, exist_ok=True)
    for f in FILES:
        target = dest / f
        if target.exists() and target.stat().st_size > 100_000:
            print(f"skip {f} (已存在 {target.stat().st_size} bytes)")
            continue
        for base in BASE_URLS:
            try:
                print(f"下载 {f} ...")
                r = requests.get(base.format(f), timeout=120)
                r.raise_for_status()
                target.write_bytes(r.content)
                print(f"  OK {len(r.content)} bytes")
                break
            except Exception as e:
                print(f"  失败 ({e})，换源重试")
        else:
            print(f"ERROR: {f} 下载失败")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
