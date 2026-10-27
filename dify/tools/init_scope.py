#!/usr/bin/env python3
"""初始化《记仇客栈》在 PowerContext 里的 Scope（以及可选的预置账本）。

一个 Scope = 一家客栈 = 一份账本。工作流里的环境变量 PC_SCOPE_ID 要填这里输出的 scope_id。

用法：
    python tools/init_scope.py --base-url http://127.0.0.1:18080
    python tools/init_scope.py --base-url http://127.0.0.1:18080 --seed data/seed_memories.json
    python tools/init_scope.py --base-url http://127.0.0.1:18080 --reset   # 新建一个干净账本

只用标准库，Python 3.11+ 直接跑，不需要装依赖。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def call(base_url: str, path: str, payload: dict[str, Any], token: str = "") -> dict[str, Any]:
    url = base_url.rstrip("/") + path
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise SystemExit(f"请求 {path} 失败：HTTP {error.code} {detail}") from error
    except urllib.error.URLError as error:
        raise SystemExit(
            f"连不上 PowerContext（{url}）：{error.reason}\n"
            "请先启动 Server：powercontext server run --env-file powercontext.env"
        ) from error


def main() -> int:
    parser = argparse.ArgumentParser(description="创建《记仇客栈》使用的 PowerContext Scope")
    parser.add_argument("--base-url", default="http://127.0.0.1:18080")
    parser.add_argument("--token", default="", help="PowerContext 开启鉴权时的 Bearer Token")
    parser.add_argument("--title", default="记仇客栈 · 老陈的账本")
    parser.add_argument("--summary", default="《记仇客栈》演示用 Scope：保存玩家与 NPC 老陈之间的全部互动账目。")
    parser.add_argument("--seed", type=Path, help="可选：预置账本的 JSON 文件")
    parser.add_argument("--reset", action="store_true", help="用新 idempotency key 建一个全新 Scope")
    args = parser.parse_args()

    key = f"grudge-inn-{int(time.time())}" if args.reset else "grudge-inn-demo-v1"
    scope = call(
        args.base_url,
        "/v1/scopes",
        {"title": args.title, "summary": args.summary, "idempotency_key": key},
        args.token,
    )
    scope_id = scope["scope_id"]
    print(f"scope_id: {scope_id}")

    seeded = 0
    if args.seed:
        memories = json.loads(args.seed.read_text(encoding="utf-8"))
        for item in memories:
            call(
                args.base_url,
                "/v1/memory/remember",
                {
                    "scope_id": scope_id,
                    "kind": item.get("kind", "event"),
                    "text": item["text"],
                    "reason": item.get("reason", "seed"),
                },
                args.token,
            )
            seeded += 1
        print(f"已写入预置记忆 {seeded} 条")

    print()
    print("把下面这行填进 Dify 应用的【环境变量 PC_SCOPE_ID】：")
    print(scope_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
