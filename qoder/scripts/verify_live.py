#!/usr/bin/env python
"""live 模式端到端验证：对已启动的应用执行一遍主流程，并打印可核对的证据。

前置：
    docker compose up -d seekdb
    powercontext server run
    python app/server.py

运行：
    python scripts/verify_live.py
    python scripts/verify_live.py --base http://127.0.0.1:8787 --question "RRF 的 k 值怎么选？"
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

RESULTS: list[tuple[bool, str]] = []


def request(base: str, method: str, path: str, payload: dict | None = None, timeout: int = 120):
    data = None
    headers = {"Content-Type": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(f"{base}{path}", data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def check(condition: bool, message: str) -> None:
    RESULTS.append((bool(condition), message))
    print(f"  {'PASS' if condition else 'FAIL'}  {message}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8787")
    parser.add_argument(
        "--question",
        default="混合检索里 RRF 融合的 k 值应该怎么选？有实验数据吗？",
    )
    args = parser.parse_args()
    base = args.base.rstrip("/")

    print(f"\n[1/6] 健康检查 {base}")
    try:
        health = request(base, "GET", "/api/health")
    except Exception as exc:  # noqa: BLE001
        print(f"  FAIL  无法访问应用：{exc}")
        return 1
    check(health.get("seekdb", {}).get("ok") is True, f"seekdb 已连接（{health['seekdb'].get('version')}）")
    check(health.get("powercontext", {}).get("ok") is True, f"PowerContext 已连接（scope={health.get('scope_id')}）")
    check(health.get("seekdb", {}).get("fts_available") is True, "全文索引可用（不是 LIKE 退化路径）")
    if not health.get("seekdb", {}).get("ok"):
        return 1

    print("\n[2/6] 文献库")
    papers = request(base, "GET", "/api/papers").get("papers", [])
    if not papers:
        imported = request(base, "POST", "/api/samples/import", {})
        print(f"  文献库为空，已导入 {imported.get('count', 0)} 篇示例材料")
        papers = request(base, "GET", "/api/papers").get("papers", [])
    check(len(papers) >= 1, f"文献库有 {len(papers)} 篇材料")

    print("\n[3/6] 混合检索问答")
    answer = request(base, "POST", "/api/ask", {"question": args.question, "top_k": 6})
    citations = answer.get("citations", [])
    check(len(answer.get("answer", "")) > 40, f"回答长度 {len(answer.get('answer', ''))} 字")
    check(len(citations) >= 1, f"返回 {len(citations)} 条带出处引用")
    check(len(answer.get("memories", [])) >= 1, f"命中 {len(answer.get('memories', []))} 条研究记忆")
    if citations:
        first = citations[0]
        print(f"  首条引用：{first['label']}（chunk_id={first['chunk_id']}，通道={'+'.join(first['matched_by'])}）")
    print(
        f"  检索通道：向量候选 {answer.get('search', {}).get('vector_candidates')}，"
        f"全文候选 {answer.get('search', {}).get('fts_candidates')}，"
        f"耗时 {answer.get('search', {}).get('latency_ms')} ms"
    )

    print("\n[4/6] 记忆写入与复用")
    marker = "验证脚本写入：每周五整理组会周报，先列结论再列证据。"
    request(base, "POST", "/api/memories", {"text": marker, "kind": "preference", "reason": "verify_live"})
    memories = request(base, "GET", "/api/memories").get("memories", [])
    check(any(marker in item.get("text", "") for item in memories), "记忆已写入并可列出")
    recall = request(base, "POST", "/api/ask", {"question": "组会周报应该怎么整理？", "top_k": 3})
    check(
        any(marker[:12] in item.get("text", "") for item in recall.get("memories", [])),
        "新问题能召回刚写入的记忆",
    )

    print("\n[5/6] 组会周报")
    weekly = request(base, "GET", "/api/weekly?days=7")
    markdown = weekly.get("markdown", "")
    check("科研周报" in markdown, "周报包含标题")
    check("关键发现" in markdown and "[1]" in markdown, "周报包含带出处的关键发现")
    check(len(weekly.get("citations", [])) >= 1, f"周报引用 {len(weekly.get('citations', []))} 条证据")

    print("\n[6/6] 技术核验日志")
    logs = request(base, "GET", "/api/logs?limit=10")
    retrieval = logs.get("retrieval", [])
    calls = logs.get("powercontext", [])
    check(len(retrieval) >= 1, f"seekdb 检索日志 {len(retrieval)} 条")
    check(len(calls) >= 1, f"PowerContext 调用记录 {len(calls)} 条")
    if retrieval:
        latest = retrieval[0]
        print(
            f"  最新检索：{latest['query'][:40]}… 通道={latest['mode']} "
            f"融合={len(latest['fused_ids'])} 条 耗时={latest['latency_ms']}ms"
        )
    if calls:
        latest = calls[0]
        print(f"  最新记忆调用：{latest['op']} status={latest['status']} {latest['duration_ms']}ms")

    failed = [message for ok, message in RESULTS if not ok]
    print("\n" + "=" * 60)
    if failed:
        print(f"结果：FAIL（{len(failed)} 项未通过）")
        for message in failed:
            print(f"  - {message}")
        return 1
    print(f"结果：PASS（{len(RESULTS)} 项全部通过）")
    print("以上检索日志与记忆调用均可在页面「技术核验」中复查。")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')[:500]}")
        raise SystemExit(1)
