"""读取 config.yaml → 简单命名空间。"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import paths


@dataclass
class Company:
    code: str
    name: str
    org_id: str
    market: str = "sse"
    listed_date: str | None = None
    note: str | None = None


@dataclass
class Config:
    companies: list[Company] = field(default_factory=list)
    fiscal_years: list[int] = field(default_factory=list)
    prospectus_fallback: dict[str, list[str]] = field(default_factory=dict)
    download: dict[str, Any] = field(default_factory=dict)
    qc: dict[str, Any] = field(default_factory=dict)

    def company(self, code: str) -> Company | None:
        return next((c for c in self.companies if c.code == code), None)


def load(path: Path | None = None) -> Config:
    p = path or (paths.PROJECT_ROOT / "config.yaml")
    with open(p, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    cfg = Config(
        companies=[Company(**c) for c in raw.get("companies", [])],
        fiscal_years=[int(y) for y in raw.get("fiscal_years", [])],
        prospectus_fallback=raw.get("prospectus_fallback", {}) or {},
        download=raw.get("download", {}) or {},
        qc=raw.get("qc", {}) or {},
    )
    return cfg


def load_env() -> dict[str, str]:
    """读取 .env（KEY=VALUE），环境变量优先。"""
    import os
    env: dict[str, str] = {}
    env_path = paths.PROJECT_ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    for k in list(env):
        if k in os.environ:
            env[k] = os.environ[k]
    return env


def datasink_key() -> str:
    """datasinking API key：.env DATASINK_API_KEY 或环境变量。"""
    key = load_env().get("DATASINK_API_KEY", "")
    if not key:
        raise RuntimeError("缺少 DATASINK_API_KEY（请在 .env 配置）")
    return key


def fmp_symbol(code: str) -> str:
    """A股代码 → FMP 风格 symbol：6xx→.SS，0xx/3xx→.SZ。"""
    if code.startswith(("6", "9")):
        return f"{code}.SS"
    return f"{code}.SZ"
