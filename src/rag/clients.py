"""API 客户端封装：DeepSeek（生成/评测）+ SiliconFlow（bge-m3 嵌入、bge-reranker 重排）。

key 从 .env 读取（DEEPSEEK_API_KEY / SILICONFLOW_API_KEY）。OpenAI 兼容协议。
"""
from __future__ import annotations

import time
from functools import lru_cache

from openai import OpenAI

from ..config import load_env

DEEPSEEK_BASE = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-chat"
SILICONFLOW_BASE = "https://api.siliconflow.cn/v1"
EMBED_MODEL = "BAAI/bge-m3"
RERANK_MODEL = "BAAI/bge-reranker-v2-m3"


@lru_cache(maxsize=1)
def deepseek() -> OpenAI:
    return OpenAI(api_key=load_env().get("DEEPSEEK_API_KEY", ""), base_url=DEEPSEEK_BASE)


@lru_cache(maxsize=1)
def siliconflow() -> OpenAI:
    return OpenAI(api_key=load_env().get("SILICONFLOW_API_KEY", ""), base_url=SILICONFLOW_BASE)


def embed(texts: list[str], batch: int = 32, retries: int = 3) -> list[list[float]]:
    """bge-m3 批量嵌入（batch 条/次，限速 0.3s，指数退避重试）。返回 1024 维向量。"""
    client = siliconflow()
    out: list[list[float]] = []
    for i in range(0, len(texts), batch):
        chunk = texts[i:i + batch]
        for attempt in range(1, retries + 1):
            try:
                resp = client.embeddings.create(model=EMBED_MODEL, input=chunk)
                out.extend([d.embedding for d in resp.data])
                break
            except Exception as e:
                if attempt == retries:
                    raise RuntimeError(f"embed 失败({attempt}次): {e}")
                time.sleep(2 ** attempt)
        time.sleep(0.3)
    return out


def rerank(query: str, docs: list[str], top_n: int = 5, retries: int = 1,
           timeout: int = 12) -> list[tuple[int, float]]:
    """bge-reranker-v2-m3 重排。返回 [(文档下标, 相关性分数)] 按分数降序。

    timeout 较短：API 变慢时快速失败，由调用方退回 RRF 结果（检索仍可用）。
    """
    import requests
    key = load_env().get("SILICONFLOW_API_KEY", "")
    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(
                SILICONFLOW_BASE + "/rerank",
                headers={"Authorization": f"Bearer {key}"},
                json={"model": RERANK_MODEL, "query": query, "documents": docs,
                      "top_n": min(top_n, len(docs)), "return_documents": False},
                timeout=timeout,
            )
            resp.raise_for_status()
            results = resp.json().get("results", [])
            pairs = [(r["index"], r["relevance_score"]) for r in results]
            # API 不保证按分数排序，必须显式降序
            pairs.sort(key=lambda x: -x[1])
            return pairs
        except Exception as e:
            if attempt == retries:
                raise RuntimeError(f"rerank 失败({attempt}次): {e}")
            time.sleep(2 ** attempt)
    return []


def chat(messages: list[dict], temperature: float = 0.1, max_tokens: int = 1024,
         retries: int = 3) -> str:
    """DeepSeek chat 单轮（requests 直连，绕开 openai SDK 的 httpx 版本问题）。"""
    import requests
    key = load_env().get("DEEPSEEK_API_KEY", "")
    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(
                DEEPSEEK_BASE + "/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json={"model": DEEPSEEK_MODEL, "messages": messages,
                      "temperature": temperature, "max_tokens": max_tokens},
                timeout=120,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"] or ""
        except Exception as e:
            if attempt == retries:
                raise RuntimeError(f"chat 失败({attempt}次): {e}")
            time.sleep(2 ** attempt)
    return ""


def chat_tools(messages: list[dict], tools: list[dict] | None = None,
               temperature: float = 0.1, max_tokens: int = 1500,
               retries: int = 3) -> dict:
    """DeepSeek function calling：返回完整 message（content 可能为 None，但 tool_calls 非空）。

    tools 为空时退化为普通对话（用于强制总结的最后一轮）。
    """
    import requests
    key = load_env().get("DEEPSEEK_API_KEY", "")
    payload: dict = {"model": DEEPSEEK_MODEL, "messages": messages,
                     "temperature": temperature, "max_tokens": max_tokens}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(
                DEEPSEEK_BASE + "/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json=payload, timeout=120,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]
        except Exception as e:
            if attempt == retries:
                raise RuntimeError(f"chat_tools 失败({attempt}次): {e}")
            time.sleep(2 ** attempt)
    return {}
