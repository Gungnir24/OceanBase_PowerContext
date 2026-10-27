#!/usr/bin/env python3
"""在**真实的 Dify**上跑一遍《记仇客栈》，并把结果落成证据文件。

它做四件事（全部走 Dify 自己的 HTTP API，等价于在网页上点一遍）：
1. 用管理员账号登录控制台，导入 `dsl/grudge-inn.yml`；
2. 把 LLM 节点指到你自己的模型（--provider / --model），并把 PowerContext 的 Scope 写进环境变量；
3. 发布应用、建一个 API Key；
4. 跑 4 轮连续对话 + 1 轮全新会话，抓每轮的台词、记仇值、节点轨迹，写成 md/json。

前置条件：
- Dify 已启动（默认 http://localhost），已完成初始化（有管理员账号）；
- 已装 `langgenius/openai_api_compatible` 插件，并给你的模型建好自定义凭据；
- PowerContext 已启动，`--scope` 指向一个**干净的** Scope；
- 如果 PowerContext 跑在宿主机（host.docker.internal），Dify 侧要放行：
  `SSRF_PROXY_ALLOW_PRIVATE_IPS=<网关网段>`，见 docs/01-部署与运行.md。

用法：
    python tools/dify_live_test.py --scope scp_xxx --password '你的 Dify 密码'
    python tools/dify_live_test.py --scope scp_xxx --password '...' \
        --provider langgenius/openai_api_compatible/openai_api_compatible \
        --model deepseek/deepseek-v4.1-flash --out evidence
退出码 0 = 5 轮全部跑通且每轮只有 1 条账本行。
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import httpx
except ImportError:
    raise SystemExit("需要 httpx：pip install httpx")

ROOT = Path(__file__).resolve().parent.parent
DSL = ROOT / "dsl" / "grudge-inn.yml"

TURNS = [
    "老板，来碗阳春面。",
    "说真的，上次我把你汤里的盐换成了糖，你居然没发现。",
    "我还把你养的那只鹦鹉教得会骂人了。",
    "对不起老板，我错了，这顿我请，再帮你劈一上午柴。",
]
CROSS_SESSION = "老板，还认得我吗？"


def login(base_url: str, email: str, password: str) -> httpx.Client:
    client = httpx.Client(base_url=base_url, timeout=600, follow_redirects=True)
    # Dify 前端会把密码做 base64 再发，这里保持一致；CSRF 走 cookie + 头。
    payload = {
        "email": email,
        "password": base64.b64encode(password.encode()).decode(),
        "remember_me": True,
        "language": "zh-Hans",
    }
    response = client.post("/console/api/login?language=zh-Hans", json=payload)
    if response.status_code >= 300:
        raise SystemExit(f"登录失败 {response.status_code}: {response.text[:300]}")
    client.headers["X-CSRF-Token"] = client.cookies.get("csrf_token") or ""
    return client


def import_and_publish(console: httpx.Client, provider: str, model: str, scope: str) -> str:
    imported = console.post(
        "/console/api/apps/imports",
        json={"mode": "yaml-content", "yaml_content": DSL.read_text(encoding="utf-8")},
    )
    imported.raise_for_status()
    result = imported.json()
    print(f"导入：{result['status']}（DSL {result['imported_dsl_version']}，警告 {len(result['warnings'])} 条）")
    app_id = result["app_id"]

    draft = console.get(f"/console/api/apps/{app_id}/workflows/draft").json()
    graph = draft["graph"]
    llm = next(node for node in graph["nodes"] if node["data"]["type"] == "llm")
    llm["data"]["model"]["provider"] = provider
    llm["data"]["model"]["name"] = model

    patch = [
        {
            "id": item["id"],
            "name": item["name"],
            "value": scope,
            "value_type": item.get("value_type", "string"),
            "description": item.get("description", ""),
            "selector": ["env", item["name"]],
        }
        for item in draft.get("environment_variables") or []
        if item["name"] == "PC_SCOPE_ID"
    ]
    synced = console.post(
        f"/console/api/apps/{app_id}/workflows/draft",
        json={
            "graph": graph,
            "features": draft.get("features") or {},
            "hash": draft.get("hash"),
            "force": False,
            "environment_variable_patch": {"environment_variables": patch, "deleted_environment_variable_ids": []},
            "conversation_variables": draft.get("conversation_variables") or [],
        },
    )
    synced.raise_for_status()
    published = console.post(f"/console/api/apps/{app_id}/workflows/publish")
    published.raise_for_status()
    print(f"发布：{published.status_code} app_id={app_id}")
    return app_id


def chat(api: httpx.Client, query: str, conversation_id: str | None) -> dict[str, Any]:
    body: dict[str, Any] = {"inputs": {}, "query": query, "response_mode": "blocking", "user": "judge-001"}
    if conversation_id:
        body["conversation_id"] = conversation_id
    started = time.time()
    response = api.post("/v1/chat-messages", json=body)
    elapsed = round(time.time() - started, 2)
    if response.status_code >= 300:
        raise SystemExit(f"对话失败 {response.status_code}: {response.text[:500]}")
    data = response.json()
    data["_elapsed"] = elapsed
    return data


def fetch_runs(console: httpx.Client, app_id: str) -> list[dict[str, Any]]:
    listing = console.get(
        f"/console/api/apps/{app_id}/advanced-chat/workflow-runs",
        params={"page": 1, "limit": 20, "triggered_from": "app-run"},
    )
    if listing.status_code != 200:
        return []
    items = sorted(listing.json().get("data") or [], key=lambda item: item.get("created_at") or 0)
    runs = []
    for item in items:
        detail = console.get(f"/console/api/apps/{app_id}/workflow-runs/{item['id']}/node-executions")
        nodes = detail.json().get("data") or [] if detail.status_code == 200 else []
        runs.append(
            {
                "id": item["id"],
                "status": item.get("status"),
                "elapsed": item.get("elapsed_time"),
                "total_tokens": item.get("total_tokens"),
                "node_order": [node.get("node_id") for node in nodes],
                "nodes": [
                    {"node": node.get("node_id"), "status": node.get("status"), "elapsed": node.get("elapsed_time")}
                    for node in nodes
                ],
            }
        )
    return runs


def ledger_texts(scope: str, pc_url: str) -> list[str]:
    try:
        response = httpx.post(f"{pc_url}/v1/memory/entries/list", json={"scope_id": scope, "limit": 100}, timeout=30)
        entries = response.json().get("entries") or []
    except Exception:
        return []
    texts = [entry.get("text") or "" for entry in entries]

    def order(text: str) -> int:
        digits = ""
        for char in text:
            if char.isdigit():
                digits += char
            elif digits:
                break
        return int(digits or 0)

    return sorted(texts, key=order)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost", help="Dify 地址")
    parser.add_argument("--pc-url", default="http://127.0.0.1:18080", help="PowerContext 地址（用于回读账本）")
    parser.add_argument("--email", default="judge@example.com")
    parser.add_argument("--password", required=True)
    parser.add_argument("--scope", required=True, help="PowerContext Scope ID")
    parser.add_argument("--provider", default="langgenius/openai_api_compatible/openai_api_compatible")
    parser.add_argument("--model", default="deepseek/deepseek-v4.1-flash")
    parser.add_argument("--out", default=str(ROOT / "evidence"))
    args = parser.parse_args()

    console = login(args.base_url, args.email, args.password)
    app_id = import_and_publish(console, args.provider, args.model, args.scope)
    token = console.post(f"/console/api/apps/{app_id}/api-keys", json={"type": "app"}).json()["token"]
    api = httpx.Client(base_url=args.base_url, timeout=600, headers={"Authorization": f"Bearer {token}"})

    records: list[dict[str, Any]] = []
    conversation_id = None
    for index, query in enumerate(TURNS, start=1):
        data = chat(api, query, conversation_id)
        conversation_id = data.get("conversation_id") or conversation_id
        answer = data.get("answer") or ""
        records.append(
            {
                "turn": index,
                "query": query,
                "answer": answer,
                "elapsed": data["_elapsed"],
                "ledger_lines": answer.count("📒"),
                "conversation_id": conversation_id,
            }
        )
        print(f"\n=== 第 {index} 轮（{data['_elapsed']}s，账本行 {answer.count('📒')} 条）\n{answer}")

    fresh = chat(api, CROSS_SESSION, None)
    fresh_answer = fresh.get("answer") or ""
    records.append(
        {
            "turn": 5,
            "query": CROSS_SESSION + "（全新会话）",
            "answer": fresh_answer,
            "elapsed": fresh["_elapsed"],
            "ledger_lines": fresh_answer.count("📒"),
            "conversation_id": fresh.get("conversation_id"),
        }
    )
    print(f"\n=== 新会话（{fresh['_elapsed']}s，账本行 {fresh_answer.count('📒')} 条）\n{fresh_answer}")

    runs = fetch_runs(console, app_id)
    ledger = ledger_texts(args.scope, args.pc_url)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": stamp,
        "dify": args.base_url,
        "app_id": app_id,
        "scope_id": args.scope,
        "provider": args.provider,
        "model": args.model,
        "records": records,
        "runs": runs,
        "ledger": ledger,
    }
    (out_dir / f"dify-live-{stamp}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        f"# Dify 实机跑通记录（{stamp}）",
        "",
        f"- Dify：{args.base_url}（应用 `{app_id}`）",
        f"- 模型：`{args.provider}` / `{args.model}`",
        f"- PowerContext Scope：`{args.scope}`",
        "",
        "## 五轮对话",
        "",
    ]
    for record in records:
        lines += [
            f"### 第 {record['turn']} 轮（{record['elapsed']}s，账本行 {record['ledger_lines']} 条）",
            "",
            f"> 玩家：{record['query']}",
            "",
            "```text",
            record["answer"].rstrip(),
            "```",
            "",
        ]
    lines += ["## 节点轨迹（Dify 服务端记录）", ""]
    for run in runs:
        lines.append(
            f"- `{run['status']}` {round(run['elapsed'] or 0, 2)}s tokens={run['total_tokens']}：{' → '.join(run['node_order'])}"
        )
    lines += ["", "## PowerContext 账本回读", "", "```text", *[text for text in ledger], "```", ""]
    (out_dir / f"dify-live-{stamp}.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n证据已写入：{out_dir / f'dify-live-{stamp}.md'}")

    bad = [record for record in records if record["ledger_lines"] != 1]
    if bad:
        print(f"注意：{len(bad)} 轮出现了重复/缺失账本行")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
