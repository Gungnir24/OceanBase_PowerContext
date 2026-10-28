"""MiniMax 客户端（OpenAI 兼容 Chat Completions）。

本作品里 MiniMax 承担「按上下文写业务代码」这一步：输入是 sddctx 装配出的
规格/代码/测试上下文 + 真实源文件，输出是结构化补丁。请求与原文都会落盘到
``docs/minimax/``，评审可以逐轮对照最终 diff。

只用标准库，避免额外依赖；任何 OpenAI 兼容网关（官方 / 聚合网关）都能替换。
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request

from .config import MinimaxConfig


class MinimaxError(RuntimeError):
    pass


FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.S)


class MiniMaxClient:
    def __init__(self, cfg: MinimaxConfig) -> None:
        self.cfg = cfg

    # ---------------------------------------------------------------- 请求
    def chat(
        self,
        messages: list[dict],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> dict:
        if not self.cfg.api_key:
            raise MinimaxError(
                "未配置 MiniMax API Key：请设置 MINIMAX_API_KEY（或 PPIO_API_KEY），"
                "参考 .env.example。"
            )
        payload = {
            "model": self.cfg.model,
            "messages": messages,
            "temperature": self.cfg.temperature if temperature is None else temperature,
            "max_tokens": self.cfg.max_tokens if max_tokens is None else max_tokens,
            "stream": False,
        }
        request = urllib.request.Request(
            f"{self.cfg.base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.cfg.api_key}",
            },
        )
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=self.cfg.timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise MinimaxError(f"MiniMax HTTP {exc.code}：{detail}") from None
        except Exception as exc:  # noqa: BLE001
            raise MinimaxError(f"调用 MiniMax 失败：{exc}") from exc

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MinimaxError(f"MiniMax 返回非 JSON：{raw[:300]}") from exc

        choices = data.get("choices") or []
        if not choices:
            raise MinimaxError(f"MiniMax 响应缺少 choices：{raw[:300]}")
        message = choices[0].get("message") or {}
        content = message.get("content") or ""
        reasoning = message.get("reasoning_content") or ""
        return {
            "content": content,
            "reasoning_content": reasoning,
            "model": data.get("model", self.cfg.model),
            "usage": data.get("usage") or {},
            "elapsed_ms": elapsed_ms,
            "raw": data,
        }

    # ---------------------------------------------------------------- JSON
    def chat_json(self, messages: list[dict], **kwargs) -> tuple[dict, dict]:
        result = self.chat(messages, **kwargs)
        return parse_json_block(result["content"]), result


def parse_json_block(content: str) -> dict:
    """从模型输出里取出 JSON 对象（容忍 ```json 围栏与前后说明文字）。"""
    text = (content or "").strip()
    fenced = FENCE_RE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    if text.startswith("{"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    start = text.find("{")
    if start == -1:
        raise MinimaxError(f"模型输出里没有 JSON 对象：{content[:200]}")
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                blob = text[start : index + 1]
                try:
                    return json.loads(blob)
                except json.JSONDecodeError as exc:
                    raise MinimaxError(f"模型输出的 JSON 无法解析：{exc}；片段：{blob[:300]}") from None
    raise MinimaxError(f"模型输出的 JSON 不完整：{content[:200]}")
