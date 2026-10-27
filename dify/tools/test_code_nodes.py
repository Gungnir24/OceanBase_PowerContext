#!/usr/bin/env python3
"""针对 DSL 里两段 Code 节点的单元测试（不需要 PowerContext、不需要模型、不需要 Dify）。

测什么：
- `整理账本`：空账本、条目乱序（同一秒写入的真实情况）、模型/人工插入的脏数据、
  超过提示词上限的条目数、状态为 inactive 的条目；
- `记账`：纯 JSON、```json 围栏、JSON 混在闲聊里、完全不是 JSON、delta 越界、
  负数记仇值、字段缺失。

用法：
    python tools/test_code_nodes.py
退出码 0 = 全部通过。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    raise SystemExit("需要 PyYAML：pip install pyyaml")

ROOT = Path(__file__).resolve().parent.parent
DSL = ROOT / "dsl" / "grudge-inn.yml"

passed = 0
failed = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  [PASS] {name}")
    else:
        failed += 1
        print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))


def compile_node(code: str) -> Any:
    namespace: dict[str, Any] = {}
    exec(compile(code, "<dify-code-node>", "exec"), namespace)  # noqa: S102
    return namespace["main"]


def entry(text: str, state: str | None = "active") -> dict[str, Any]:
    item = {"text": text}
    if state is not None:
        item["state"] = state
    return item


def ledger(*entries: dict[str, Any]) -> str:
    return json.dumps({"entries": list(entries)}, ensure_ascii=False)


def prepared(items: list[str] | None = None, status: str = "ready") -> str:
    payload = {"items": [{"content": text} for text in (items or [])]}
    content = "BEGIN_POWERCONTEXT_PREPARED_CONTEXT_V1\n" + json.dumps(payload, ensure_ascii=False) + "\nEND_POWERCONTEXT_PREPARED_CONTEXT_V1"
    return json.dumps({"status": status, "content": content, "content_bytes": len(content)}, ensure_ascii=False)


def main() -> int:
    data = yaml.safe_load(DSL.read_text(encoding="utf-8"))
    nodes = {node["id"]: node["data"] for node in data["workflow"]["graph"]["nodes"]}
    build = compile_node(nodes["code_build"]["code"])
    parse = compile_node(nodes["code_parse"]["code"])

    print("== 整理账本 ==")

    out = build(ledger_body="{}", prepared_body="{}")
    check("空账本：记仇值 0", out["grudge"] == 0, str(out))
    check("空账本：笔数 0", out["count"] == 0)
    check("空账本：提示词给出空账本说明", "空" in out["ledger_text"])
    check("空账本：has_history=no", out["has_history"] == "no")

    newest_first = ledger(
        entry("[第3笔｜记仇值 53/100] 玩家说：鹦鹉；老陈：记下了。"),
        entry("[第2笔｜记仇值 27/100] 玩家说：糖；老陈：记下了。"),
        entry("[第1笔｜记仇值 1/100] 玩家说：面；老陈：记下了。"),
    )
    out = build(ledger_body=newest_first, prepared_body="{}")
    check("新到旧：记仇值取最新", out["grudge"] == 53, str(out["grudge"]))
    check("新到旧：笔数取最大编号", out["count"] == 3)
    check("新到旧：首行是第 3 笔", "第3笔" in out["ledger_text"].splitlines()[0], out["ledger_text"][:40])

    oldest_first = ledger(
        entry("[第1笔｜记仇值 1/100] 玩家说：面；老陈：记下了。"),
        entry("[第2笔｜记仇值 27/100] 玩家说：糖；老陈：记下了。"),
        entry("[第3笔｜记仇值 53/100] 玩家说：鹦鹉；老陈：记下了。"),
    )
    out = build(ledger_body=oldest_first, prepared_body="{}")
    check("旧到新（列表乱序）：记仇值仍取最新", out["grudge"] == 53, str(out["grudge"]))
    check("旧到新（列表乱序）：首行是第 3 笔", "第3笔" in out["ledger_text"].splitlines()[0])

    shuffled = ledger(
        entry("[第2笔｜记仇值 27/100] 玩家说：糖；老陈：记下了。"),
        entry("[第1笔｜记仇值 1/100] 玩家说：面；老陈：记下了。"),
        entry("[第4笔｜记仇值 31/100] 玩家说：道歉；老陈：算了。"),
        entry("[第3笔｜记仇值 53/100] 玩家说：鹦鹉；老陈：记下了。"),
    )
    out = build(ledger_body=shuffled, prepared_body="{}")
    check("完全乱序：记仇值取第 4 笔", out["grudge"] == 31, str(out["grudge"]))
    check("完全乱序：笔数 4", out["count"] == 4)
    check("完全乱序：只输出前 12 条", len(out["ledger_text"].splitlines()) == 4)

    out = build(
        ledger_body=ledger(
            entry("[第9笔｜记仇值 80/100] 玩家说：砸店；老陈：报官。", state="inactive"),
            entry("[第8笔｜记仇值 60/100] 玩家说：赖账；老陈：记下。"),
            entry("[第7笔｜记仇值 40/100] 玩家说：闲聊；老陈：嗯。"),
        ),
        prepared_body="{}",
    )
    check("inactive 条目被跳过：记仇值取 60", out["grudge"] == 60, str(out["grudge"]))
    check("inactive 条目被跳过：笔数 8", out["count"] == 8)

    out = build(
        ledger_body=ledger(
            entry("[第5笔] 玩家说：没写记仇值；老陈：……"),
            entry("[第4笔｜记仇值 70/100] 玩家说：有记仇值；老陈：记下。"),
        ),
        prepared_body="{}",
    )
    check("条目缺记仇值：回退到下一条带记仇值的", out["grudge"] == 70, str(out["grudge"]))

    out = build(
        ledger_body=ledger(*[entry(f"[第{i}笔｜记仇值 {i}/100] 玩家说：第{i}次；老陈：记下。") for i in range(1, 21)]),
        prepared_body="{}",
    )
    check("20 条账本：提示词只放 12 条", len(out["ledger_text"].splitlines()) == 12)
    check("20 条账本：记仇值取第 20 笔", out["grudge"] == 20, str(out["grudge"]))

    out = build(ledger_body="这个接口返回了 HTML，不是 JSON", prepared_body="not json")
    check("脏响应：不抛异常且记仇值为 0", out["grudge"] == 0, str(out))

    out = build(ledger_body=ledger(entry("[第2笔｜记仇值 999/100] 玩家说：离谱；老陈：……")), prepared_body="{}")
    check("记仇值越界被截断到 100", out["grudge"] == 100, str(out["grudge"]))

    out = build(ledger_body=newest_first, prepared_body=prepared(["上次他把盐换成了糖", "他把鹦鹉教得会骂人"]))
    check("prepare 结果被整理成要点", "鹦鹉" in out["prepared_text"] and "盐" in out["prepared_text"], out["prepared_text"][:60])

    out = build(ledger_body=ledger(entry("[第1笔｜记仇值 5/100] 玩家说：你好；老陈：嗯。")), prepared_body=prepared([], status="empty"))
    check("prepare 为空时给出占位文案", "没想起" in out["prepared_text"], out["prepared_text"])

    print("\n== 记账 ==")

    out = parse(
        llm_text=json.dumps({"reply": "（擦碗）你说啥。", "delta": 20, "summary": "玩家挑衅。", "mood": "不爽"}, ensure_ascii=False),
        query="老板你真抠",
        grudge=10,
        count=3,
        scope_id="scp_test",
    )
    check("纯 JSON：台词正确", out["reply"] == "（擦碗）你说啥。")
    check("纯 JSON：记仇值累加", out["grudge_new"] == 30, str(out["grudge_new"]))
    check("纯 JSON：笔数 +1", out["count_new"] == 4)
    body = json.loads(out["body"])
    check("写回的 body 是合法 JSON", body["scope_id"] == "scp_test" and body["kind"] == "event")
    check("记忆文本带编号与记仇值", body["text"].startswith("[第4笔｜记仇值 30/100]"), body["text"])
    check("记忆文本包含玩家原话", "老板你真抠" in body["text"])

    out = parse(
        llm_text='```json\n{"reply": "哼。", "delta": -15, "summary": "玩家道歉。", "mood": "缓和"}\n```',
        query="对不起",
        grudge=50,
        count=1,
        scope_id="scp_test",
    )
    check("围栏 JSON：能解析", out["reply"] == "哼。" and out["delta"] == -15, str(out["delta"]))
    check("围栏 JSON：记仇值下降", out["grudge_new"] == 35, str(out["grudge_new"]))

    out = parse(
        llm_text='行吧，我记下了 {"reply": "（翻账本）又是你。", "delta": 5, "summary": "玩家又来了。", "mood": "麻木"} 就这样',
        query="我又来了",
        grudge=20,
        count=2,
        scope_id="scp_test",
    )
    check("JSON 混在文字里：能抠出来", out["reply"] == "（翻账本）又是你。", out["reply"])

    out = parse(llm_text="（老陈头也不抬）面在锅里。", query="来碗面", grudge=8, count=5, scope_id="scp_test")
    check("完全不是 JSON：降级为原文", out["reply"] == "（老陈头也不抬）面在锅里。")
    check("完全不是 JSON：delta 按兜底 +2", out["delta"] == 2, str(out["delta"]))
    check("完全不是 JSON：仍然记账", out["grudge_new"] == 10 and out["count_new"] == 6, str(out["grudge_new"]))

    out = parse(
        llm_text=json.dumps({"reply": "爆炸", "delta": 999, "summary": "越界", "mood": "暴走"}, ensure_ascii=False),
        query="我把店烧了",
        grudge=90,
        count=9,
        scope_id="scp_test",
    )
    check("delta 越界被裁剪到 40", out["delta"] == 40, str(out["delta"]))
    check("记仇值上限 100", out["grudge_new"] == 100, str(out["grudge_new"]))

    out = parse(
        llm_text=json.dumps({"reply": "原谅你", "delta": -999, "summary": "越界", "mood": "宽容"}, ensure_ascii=False),
        query="我给你跪下了",
        grudge=20,
        count=2,
        scope_id="scp_test",
    )
    check("delta 下限 -30", out["delta"] == -30, str(out["delta"]))
    check("记仇值下限 0", out["grudge_new"] == 0, str(out["grudge_new"]))

    out = parse(
        llm_text=json.dumps({"delta": 3}, ensure_ascii=False),
        query="嗯",
        grudge=0,
        count=0,
        scope_id="scp_test",
    )
    check("缺 reply 字段：给兜底台词", bool(out["reply"]))
    check("缺 summary 字段：给兜底摘要", bool(out["summary"]))

    out = parse(
        llm_text=json.dumps({"reply": "记下", "delta": "12", "summary": "字符串数字", "mood": "平"}, ensure_ascii=False),
        query="嗨",
        grudge=0,
        count=0,
        scope_id="scp_test",
    )
    check("delta 是字符串数字也能算", out["grudge_new"] == 12, str(out["grudge_new"]))

    out = parse(llm_text='{"reply": "半截', query="坏 JSON", grudge=5, count=1, scope_id="scp_test")
    check("坏 JSON：不抛异常", isinstance(out["reply"], str))
    check("坏 JSON：账本不丢", out["count_new"] == 2)

    # Dify 里真实踩到的坑：模型说完台词会自己补一条「📒 第 N 笔账」，answer 再追加一条就重复了。
    out = parse(
        llm_text="（把抹布一摔）又是你小子。\n\n---\n📒 第 3 笔账 ｜ 记仇值 38/100 ｜ 心情：火大",
        query="鹦鹉的事我也说了",
        grudge=20,
        count=2,
        scope_id="scp_test",
    )
    check("纯文本自带账本行：reply 里削掉", "📒" not in out["reply"], out["reply"][:60])
    check("纯文本自带账本行：分隔线也削掉", "---" not in out["reply"], out["reply"][:60])
    check("纯文本自带账本行：正文还在", "又是你小子" in out["reply"], out["reply"][:60])
    check("纯文本没有 JSON：按兜底 +2", out["delta"] == 2 and out["grudge_new"] == 22, str(out["delta"]))

    out = parse(
        llm_text="（把擀面杖搁下）哼，知道错了？这顿你请，柴也归你劈。",
        query="对不起老板，这顿我请，再帮你劈一上午柴。",
        grudge=40,
        count=3,
        scope_id="scp_test",
    )
    check("纯文本道歉：记仇值下降", out["delta"] == -12 and out["grudge_new"] == 28, str(out["delta"]))
    check("纯文本道歉：心情松口", out["mood"] == "松口", out["mood"])

    out = parse(
        llm_text="（抹布一摔）又是你小子！",
        query="上次我把你汤里的盐换成糖了。",
        grudge=20,
        count=3,
        scope_id="scp_test",
    )
    check("纯文本恶作剧：记仇值上升", out["delta"] == 12 and out["grudge_new"] == 32, str(out["delta"]))
    check("纯文本恶作剧：心情来气", out["mood"] == "来气", out["mood"])

    out = parse(
        llm_text=json.dumps(
            {
                "reply": "哼。\n\n---\n📒 第 3 笔账 ｜ 记仇值 38/100 ｜ 心情：火大",
                "delta": 5,
                "summary": "照旧",
                "mood": "火",
            },
            ensure_ascii=False,
        ),
        query="随便",
        grudge=20,
        count=2,
        scope_id="scp_test",
    )
    check("JSON 的 reply 夹账本行：也削掉", "📒" not in out["reply"], out["reply"][:60])
    check("JSON 正常解析：delta 照样生效", out["grudge_new"] == 25, str(out["grudge_new"]))

    out = parse(
        llm_text="---\n📒 第 3 笔账 ｜ 记仇值 38/100",
        query="只有一条账本行",
        grudge=10,
        count=2,
        scope_id="scp_test",
    )
    check("整段只有账本行：给兜底台词", out["reply"] == "（老陈哼了一声，没接话）", out["reply"])

    print()
    print(f"通过 {passed} 项，失败 {failed} 项")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
