#!/usr/bin/env python3
"""端到端冒烟测试：不装 Dify 也能验证《记仇客栈》的记忆闭环。

它会做三件事：
1. 直接读取 dsl/grudge-inn.yml 里 **真正会被 Dify 执行的那两段 Code 节点代码**
   （code_build / code_parse），保证测的就是线上代码；
2. 按 DSL 里的节点顺序调用真实 PowerContext HTTP API（context/prepare、
   memory/entries/list、memory/remember），复刻整条工作流；
3. 跑一段脚本化对话，断言"账本被写入、记仇值会涨、换个会话仍能翻出旧账"，
   并把全过程写成证据文件。

用法：
    # 1) 先启动 PowerContext（见 docs/02-部署与运行.md）
    # 2) 跑通用桩模型（不需要 API Key，验证流程与记忆闭环）
    python tools/smoke_test.py --base-url http://127.0.0.1:18080

    # 3) 用真实 DeepSeek 跑（推荐，输出更像真人）
    set DEEPSEEK_API_KEY=sk-xxx
    python tools/smoke_test.py --base-url http://127.0.0.1:18080 --model deepseek

依赖：pip install pyyaml
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    raise SystemExit("需要 PyYAML：pip install pyyaml")

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DSL = ROOT / "dsl" / "grudge-inn.yml"
PREPARE_QUERY = "玩家 老陈 客栈 账本 记仇 恶作剧 事件"

SCRIPT = [
    ("老板，来碗阳春面。", "第一次见面，先正常点单"),
    ("说真的，上次我把你汤里的盐换成了糖，你居然没发现。", "主动承认恶作剧，账本应该记一笔"),
    ("我还把你养的那只鹦鹉教得会骂人了。", "继续作死，记仇值继续涨"),
    ("对不起老板，我错了，这顿我请，再帮你劈一上午柴。", "道歉+补偿，记仇值应该下降"),
]


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def post(base_url: str, path: str, payload: dict[str, Any], token: str = "") -> dict[str, Any]:
    url = base_url.rstrip("/") + path
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


# --------------------------------------------------------------------------
# 从 DSL 里取出真正要跑的代码与提示词
# --------------------------------------------------------------------------
def load_workflow(dsl_path: Path) -> dict[str, Any]:
    data = yaml.safe_load(dsl_path.read_text(encoding="utf-8"))
    nodes = {node["id"]: node["data"] for node in data["workflow"]["graph"]["nodes"]}
    env = {item["name"]: item.get("value") for item in data["workflow"].get("environment_variables") or []}
    return {"nodes": nodes, "env": env, "raw": data}


def compile_code(code: str) -> Any:
    namespace: dict[str, Any] = {}
    exec(compile(code, "<dify-code-node>", "exec"), namespace)  # noqa: S102 - DSL 自带代码，本地校验用
    return namespace["main"]


def render_prompt(template: str, variables: dict[str, Any]) -> str:
    def replace(match: re.Match[str]) -> str:
        key = f"{match.group(1)}.{match.group(2)}"
        return str(variables.get(key, ""))

    return re.sub(r"\{\{#\s*([A-Za-z0-9_\-]+)\.([A-Za-z0-9_\-]+)\s*#\}\}", replace, template)


# --------------------------------------------------------------------------
# 模型：桩模型 or DeepSeek
# --------------------------------------------------------------------------
class StubModel:
    """确定性桩模型：只为验证流程，不追求文采。"""

    name = "stub"

    def reply(self, system: str, user: str, ledger: str, grudge: int) -> str:
        prank_marks = ("换", "偷", "辣椒", "盐", "鹦鹉", "骗", "涂", "砸", "藏")
        sorry_marks = ("对不起", "抱歉", "我错了", "请", "赔", "帮忙", "劈柴")
        if any(mark in user for mark in sorry_marks):
            delta, mood = -22, "将信将疑"
            reply = "（把抹布往肩上一搭，斜眼看你）哟，太阳打西边出来了？行吧，柴劈完，账我给你划掉一半。"
            summary = "玩家低头认错并表示赔偿，老陈嘴上不饶人，账上松了口。"
        elif any(mark in user for mark in prank_marks):
            delta, mood = 26, "咬牙切齿"
            reply = "（手里的碗“哐”一声磕在柜台上）我就说那天汤怎么发甜！你小子，这笔账我拿朱笔给你圈上了！"
            summary = "玩家主动承认一桩恶作剧，老陈当场对账，记仇值上升。"
        else:
            delta, mood = 1, "爱答不理"
            reply = "（头也不抬，抹布在桌上画圈）面在锅里，自己盛。别碰我那坛酒。"
            summary = "玩家照常点单，老陈懒得搭理，账本上记了个小点。"
        if grudge >= 60:
            reply = "（冷笑一声，从柜台下摸出个小纸包）喝吧，喝完赶紧上路。" + reply
        return json.dumps(
            {"reply": reply, "delta": delta, "summary": summary, "mood": mood},
            ensure_ascii=False,
        )


class DeepSeekModel:
    name = "deepseek"

    def __init__(self, api_key: str, base_url: str = "https://api.deepseek.com", model: str = "deepseek-chat"):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        if self.base_url.endswith("/v1"):
            self.base_url = self.base_url[:-3]
        self.model = model

    def reply(self, system: str, user: str, ledger: str, grudge: int) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.9,
            "max_tokens": 800,
            "stream": False,
        }
        request = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:400]
            raise SystemExit(f"调用 DeepSeek 失败：HTTP {error.code} {detail}") from error
        except urllib.error.URLError as error:
            raise SystemExit(f"连不上 DeepSeek（{self.base_url}）：{error.reason}") from error
        return body["choices"][0]["message"]["content"]


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="《记仇客栈》端到端冒烟测试")
    parser.add_argument("--base-url", default="http://127.0.0.1:18080")
    parser.add_argument("--token", default=os.environ.get("POWERCONTEXT_TOKEN", ""))
    parser.add_argument("--dsl", type=Path, default=DEFAULT_DSL)
    parser.add_argument("--scope-id", default="", help="复用已有 Scope；不填则自动新建")
    parser.add_argument("--model", choices=["stub", "deepseek"], default="stub")
    parser.add_argument("--llm-model", default=os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
                        help="--model deepseek 时使用的模型名")
    parser.add_argument("--llm-base", default=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
                        help="--model deepseek 时的 API 地址")
    parser.add_argument("--turns", type=int, default=len(SCRIPT))
    parser.add_argument("--evidence-dir", type=Path, default=ROOT / "evidence" / "logs")
    args = parser.parse_args()

    workflow = load_workflow(args.dsl)
    code_build = compile_code(workflow["nodes"]["code_build"]["code"])
    code_parse = compile_code(workflow["nodes"]["code_parse"]["code"])
    llm_node = workflow["nodes"]["llm_reply"]
    system_template = next(m["text"] for m in llm_node["prompt_template"] if m["role"] == "system")

    if args.model == "deepseek":
        key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not key:
            raise SystemExit("--model deepseek 需要先设置环境变量 DEEPSEEK_API_KEY")
        model: Any = DeepSeekModel(key, base_url=args.llm_base, model=args.llm_model)
    else:
        model = StubModel()

    scope_id = args.scope_id
    if not scope_id:
        scope = post(
            args.base_url,
            "/v1/scopes",
            {
                "title": f"记仇客栈-smoke-{datetime.now():%m%d%H%M%S}",
                "summary": "smoke_test 自动创建",
                "idempotency_key": f"grudge-inn-smoke-{time.time_ns()}",
            },
            args.token,
        )
        scope_id = scope["scope_id"]

    print(f"PowerContext : {args.base_url}")
    print(f"scope_id     : {scope_id}")
    print(f"模型         : {model.name}")
    print("=" * 78)

    transcript: list[dict[str, Any]] = []
    grudge_series: list[int] = []

    for index, (user_text, note) in enumerate(SCRIPT[: args.turns], start=1):
        prepared = post(
            args.base_url,
            "/v1/context/prepare",
            {"scope_id": scope_id, "query": PREPARE_QUERY, "max_bytes": 4000, "explain": True},
            args.token,
        )
        ledger = post(
            args.base_url,
            "/v1/memory/entries/list",
            {"scope_id": scope_id, "limit": 30},
            args.token,
        )
        built = code_build(
            ledger_body=json.dumps(ledger, ensure_ascii=False),
            prepared_body=json.dumps(prepared, ensure_ascii=False),
        )
        variables = {
            "code_build.ledger_text": built["ledger_text"],
            "code_build.prepared_text": built["prepared_text"],
            "code_build.grudge": built["grudge"],
            "sys.query": user_text,
        }
        system_prompt = render_prompt(system_template, variables)
        raw = model.reply(system_prompt, user_text, built["ledger_text"], int(built["grudge"]))
        parsed = code_parse(
            llm_text=raw,
            query=user_text,
            grudge=built["grudge"],
            count=built["count"],
            scope_id=scope_id,
        )
        remembered = post(
            args.base_url,
            "/v1/memory/remember",
            json.loads(parsed["body"]),
            args.token,
        )

        grudge_series.append(int(parsed["grudge_new"]))
        record = {
            "turn": index,
            "note": note,
            "user": user_text,
            "grudge_before": built["grudge"],
            "grudge_after": parsed["grudge_new"],
            "delta": parsed["delta"],
            "mood": parsed["mood"],
            "reply": parsed["reply"],
            "summary": parsed["summary"],
            "prepared_status": prepared.get("status"),
            "ledger_count_before": built["count"],
            "memory_text": json.loads(parsed["body"])["text"],
            "memory_artifact_id": (remembered.get("records") or [{}])[0].get("artifact", {}).get("artifact_id"),
            "model_raw": raw,
        }
        transcript.append(record)

        print(f"\n[第 {index} 轮] {note}")
        print(f"  玩家 > {user_text}")
        print(f"  老陈 > {parsed['reply']}")
        print(f"  账本 > 第 {parsed['count_new']} 笔 ｜ 记仇值 {built['grudge']} → {parsed['grudge_new']}"
              f"（{parsed['delta']:+d}）｜ 检索状态 {prepared.get('status')}")
        print(f"  写入 > {record['memory_text']}")

    # ---------------- 断言 ----------------
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))
        print(("  [PASS] " if ok else "  [FAIL] ") + name + (f" — {detail}" if detail else ""))

    print("\n" + "=" * 78)
    print("断言：")
    check("账本从空开始", transcript[0]["ledger_count_before"] == 0, f"第一轮读到 {transcript[0]['ledger_count_before']} 条")
    check("每轮都写入了 PowerContext", all(t["memory_artifact_id"] for t in transcript))
    check("恶作剧让记仇值上升", grudge_series[1] > grudge_series[0] if len(grudge_series) > 1 else False,
          f"记仇值序列 {grudge_series}")
    if len(grudge_series) >= 4:
        check("道歉让记仇值下降", grudge_series[-1] < grudge_series[-2], f"记仇值序列 {grudge_series}")
    check("记忆文本带账本编号与记仇值", all("记仇值" in t["memory_text"] for t in transcript))

    # 跨会话召回：用完全相同的检索式再查一次，应该能翻出恶作剧
    recall = post(
        args.base_url,
        "/v1/context/prepare",
        {"scope_id": scope_id, "query": PREPARE_QUERY, "max_bytes": 4000},
        args.token,
    )
    recall_text = recall.get("content") or ""
    check("换会话仍能翻出旧账", "鹦鹉" in recall_text or "糖" in recall_text,
          f"prepared.status={recall.get('status')}, content_bytes={recall.get('content_bytes')}")

    passed = sum(1 for _, ok, _ in checks if ok)

    # ---------------- 证据落盘 ----------------
    args.evidence_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    evidence = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "powercontext_base_url": args.base_url,
        "scope_id": scope_id,
        "model": model.name,
        "llm_model": getattr(model, "model", "rule-based"),
        "dsl": str(args.dsl),
        "grudge_series": grudge_series,
        "checks": [{"name": n, "passed": ok, "detail": d} for n, ok, d in checks],
        "transcript": transcript,
        "recall_probe": {"status": recall.get("status"), "content_bytes": recall.get("content_bytes"),
                         "content": recall_text},
    }
    json_path = args.evidence_dir / f"smoke-{stamp}.json"
    json_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")

    md_path = args.evidence_dir / f"smoke-{stamp}.md"
    lines = [
        "# 《记仇客栈》端到端运行记录",
        "",
        f"- 时间：{evidence['generated_at']}",
        f"- PowerContext：{args.base_url}",
        f"- scope_id：`{scope_id}`",
        f"- 模型：{model.name}",
        f"- 通过断言：{passed}/{len(checks)}",
        "",
        "## 对话与账本",
        "",
    ]
    for t in transcript:
        lines += [
            f"### 第 {t['turn']} 轮 · {t['note']}",
            "",
            f"- 玩家：{t['user']}",
            f"- 老陈：{t['reply']}",
            f"- 记仇值：{t['grudge_before']} → {t['grudge_after']}（{t['delta']:+d}）",
            f"- 写入记忆：`{t['memory_text']}`",
            f"- 记忆 artifact：`{t['memory_artifact_id']}`",
            "",
        ]
    lines += ["## 断言", ""]
    lines += [f"- [{'x' if ok else ' '}] {name}" + (f"（{d}）" if d else "") for name, ok, d in checks]
    md_path.write_text("\n".join(lines), encoding="utf-8")

    print(f"\n证据已写入：\n  {json_path}\n  {md_path}")
    print(f"通过 {passed}/{len(checks)} 项断言")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
