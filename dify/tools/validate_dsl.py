#!/usr/bin/env python3
"""离线校验 Dify DSL（YAML），在导入 Dify 之前把常见错误挡掉。

为什么需要它：Dify 导入 DSL 时不会给出友好的字段级报错，一个变量选择器写错，
可能要到运行时才炸。这个脚本按 Dify 1.x 的导入规则做静态检查：

1. 顶层结构：kind / app / workflow.graph；
2. 节点：id 唯一、type 合法、必填字段齐全、字段类型正确；
3. 连线：source/target 都存在、sourceHandle 合法；
4. 引用：`{{#node.var#}}` 的 node 必须存在（或属于 sys/env/conversation），
   var 必须真的是该节点会产出的变量；
5. 模式约束：advanced-chat 里不允许出现 end 节点。

用法：
    python tools/validate_dsl.py dsl/grudge-inn.yml
    python tools/validate_dsl.py dsl/grudge-inn.yml --strict   # 有 graphon 时用官方模型再验一遍

退出码 0 表示通过，1 表示有错误。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - 提示安装
    raise SystemExit("需要 PyYAML：pip install pyyaml")


TEMPLATE_RE = re.compile(r"\{\{#\s*([A-Za-z0-9_\-]+)\.([A-Za-z0-9_\-]+)\s*#\}\}")

ROOT_NODE_IDS = {"sys", "env", "conversation", "rag"}

# 各节点会往变量池里放的输出变量名。
NODE_OUTPUTS: dict[str, set[str]] = {
    "start": {"query", "files"},
    "http-request": {"body", "status_code", "headers", "files"},
    "llm": {"text", "usage", "reasoning_content", "files"},
    "code": set(),  # 由 data.outputs 决定
    "answer": set(),
    "if-else": set(),
    "end": set(),
    "variable-aggregator": {"output"},
    "template-transform": {"output"},
    "tool": {"text", "files", "json"},
}

SYSTEM_VARIABLES = {
    "query", "files", "conversation_id", "user_id", "dialogue_count", "app_id",
    "workflow_id", "workflow_run_id", "timestamp", "sys.locale", "locale", "env",
}

CHATFLOW_FORBIDDEN = {"end"}


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.infos: list[str] = []

    def error(self, where: str, message: str) -> None:
        self.errors.append(f"[ERROR] {where}: {message}")

    def warn(self, where: str, message: str) -> None:
        self.warnings.append(f"[WARN ] {where}: {message}")

    def info(self, message: str) -> None:
        self.infos.append(f"[INFO ] {message}")

    def dump(self) -> int:
        for line in self.infos + self.warnings + self.errors:
            print(line)
        print()
        print(f"检查完成：{len(self.errors)} 个错误，{len(self.warnings)} 个警告。")
        return 0 if not self.errors else 1


def iter_strings(value: Any, path: str = ""):
    """遍历节点 data 里所有字符串，产出 (路径, 字符串)。"""
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from iter_strings(item, f"{path}.{key}" if path else str(key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from iter_strings(item, f"{path}[{index}]")


def check_http(where: str, data: dict[str, Any], report: Report) -> None:
    for field in ("method", "url", "headers", "params"):
        if field not in data:
            report.error(where, f"HTTP 节点缺少必填字段 {field!r}")
    if data.get("method", "").upper() not in {
        "GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS",
    }:
        report.error(where, f"method 非法：{data.get('method')!r}")
    auth = data.get("authorization")
    if not isinstance(auth, dict):
        report.error(where, "authorization 必须是对象，例如 {type: no-auth}")
    elif auth.get("type") == "api-key":
        config = auth.get("config")
        if not isinstance(config, dict) or config.get("type") not in {"basic", "bearer", "custom"}:
            report.error(where, "api-key 鉴权需要 config.type ∈ {basic, bearer, custom}")
    elif auth.get("type") != "no-auth":
        report.error(where, f"authorization.type 非法：{auth.get('type')!r}")

    body = data.get("body")
    if body is not None:
        if not isinstance(body, dict) or body.get("type") not in {
            "none", "form-data", "x-www-form-urlencoded", "raw-text", "json", "binary",
        }:
            report.error(where, "body.type 非法")
        elif body.get("type") in {"json", "raw-text"}:
            items = body.get("data")
            if not isinstance(items, list) or len(items) != 1:
                report.error(where, "json/raw-text 类型的 body.data 必须恰好 1 项")
    timeout = data.get("timeout")
    if timeout is not None and not isinstance(timeout, dict):
        report.error(where, "timeout 必须是对象")


def check_json_body_template(node_id: str, data: dict[str, Any], env_vars: dict[str, Any], report: Report) -> None:
    """HTTP 节点的 JSON body 在渲染环境变量后必须是合法 JSON——这是导入后最容易运行时翻车的地方。"""
    body = data.get("body")
    if not isinstance(body, dict) or body.get("type") != "json":
        return
    items = body.get("data")
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        return
    template = items[0].get("value")
    if not isinstance(template, str):
        return
    rendered = re.sub(
        r"\{\{#\s*env\.([A-Za-z0-9_]+)\s*#\}\}",
        lambda m: str(env_vars.get(m.group(1), {}).get("value", "PLACEHOLDER")),
        template,
    )
    if "{{#" in rendered:
        report.warn(f"节点 {node_id}", "JSON body 里还有非 env 的变量，无法静态校验")
        return
    try:
        json.loads(rendered)
    except Exception as error:  # noqa: BLE001
        report.error(f"节点 {node_id}", f"JSON body 渲染后不是合法 JSON：{error}；渲染结果={rendered[:160]}")

def check_code(where: str, data: dict[str, Any], report: Report) -> None:
    if data.get("code_language") not in {"python3", "javascript"}:
        report.error(where, f"code_language 非法：{data.get('code_language')!r}")
    code = data.get("code")
    if not isinstance(code, str) or not code.strip():
        report.error(where, "code 为空")
        return
    if "def main(" not in code and "function main(" not in code:
        report.error(where, "code 里找不到 main 入口函数")
    outputs = data.get("outputs")
    if not isinstance(outputs, dict) or not outputs:
        report.error(where, "code 节点必须声明 outputs")
        return
    for name, spec in outputs.items():
        if not isinstance(spec, dict) or "type" not in spec:
            report.error(where, f"outputs.{name} 缺少 type")
    variables = data.get("variables")
    if not isinstance(variables, list):
        report.error(where, "variables 必须是列表")
        return
    declared = set()
    for item in variables:
        selector = item.get("value_selector") if isinstance(item, dict) else None
        if not isinstance(selector, list) or len(selector) != 2:
            report.error(where, f"输入变量 {item!r} 的 value_selector 必须是 [节点, 变量]")
            continue
        declared.add(item.get("variable"))
    # main() 的参数名要和输入变量名一致
    signature = re.search(r"def main\(([^)]*)\)", code, re.S)
    if signature:
        params = {
            part.split(":")[0].strip()
            for part in signature.group(1).split(",")
            if part.strip()
        }
        if params != {d for d in declared if d}:
            report.warn(where, f"main() 参数 {sorted(params)} 与输入变量 {sorted(d for d in declared if d)} 不一致")


def check_llm(where: str, data: dict[str, Any], report: Report) -> None:
    model = data.get("model")
    if not isinstance(model, dict):
        report.error(where, "model 必须是对象")
    else:
        for field in ("provider", "name", "mode"):
            if not model.get(field):
                report.error(where, f"model.{field} 为空")
        if model.get("mode") not in {"chat", "completion"}:
            report.error(where, f"model.mode 非法：{model.get('mode')!r}")
    prompt = data.get("prompt_template")
    if isinstance(prompt, list):
        for index, item in enumerate(prompt):
            if not isinstance(item, dict) or item.get("role") not in {"system", "user", "assistant"}:
                report.error(where, f"prompt_template[{index}] 角色非法")
            elif not isinstance(item.get("text"), str):
                report.error(where, f"prompt_template[{index}].text 必须是字符串")
    elif not isinstance(prompt, dict):
        report.error(where, "prompt_template 必须是消息列表或补全模板对象")
    context = data.get("context")
    if not isinstance(context, dict) or "enabled" not in context:
        report.error(where, "context 必须是 {enabled: bool, variable_selector: []}")


def check_start(where: str, data: dict[str, Any], report: Report) -> None:
    if not isinstance(data.get("variables"), list):
        report.error(where, "start.variables 必须是列表（没有输入就写 []）")


def check_answer(where: str, data: dict[str, Any], report: Report) -> None:
    if not isinstance(data.get("answer"), str):
        report.error(where, "answer 必须是字符串")


NODE_CHECKERS = {
    "start": check_start,
    "http-request": check_http,
    "code": check_code,
    "llm": check_llm,
    "answer": check_answer,
}


def validate(data: dict[str, Any], *, strict: bool = False) -> Report:
    report = Report()

    if data.get("kind") != "app":
        report.error("top", "kind 必须是 'app'")
    if not isinstance(data.get("version"), str):
        report.error("top", "version 必须是字符串，例如 '0.7.0'")

    app = data.get("app")
    if not isinstance(app, dict):
        report.error("top", "缺少 app 段")
        return report
    mode = app.get("mode")
    if mode not in {"advanced-chat", "workflow", "chat", "agent-chat", "completion"}:
        report.error("app", f"mode 非法：{mode!r}")
    if not app.get("name"):
        report.error("app", "name 为空")

    workflow = data.get("workflow")
    if not isinstance(workflow, dict):
        report.error("top", "缺少 workflow 段")
        return report
    graph = workflow.get("graph")
    if not isinstance(graph, dict):
        report.error("workflow", "缺少 graph")
        return report

    env_vars = {}
    for item in workflow.get("environment_variables") or []:
        if isinstance(item, dict) and item.get("name"):
            env_vars[item["name"]] = item
            if item.get("value_type") not in {"string", "secret", "number", "object"}:
                report.error("workflow.environment_variables", f"{item['name']} 的 value_type 非法")

    nodes = graph.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        report.error("graph", "nodes 必须是非空列表")
        return report

    by_id: dict[str, dict[str, Any]] = {}
    outputs: dict[str, set[str]] = {}
    for index, node in enumerate(nodes):
        where = f"nodes[{index}]"
        if not isinstance(node, dict):
            report.error(where, "节点必须是对象")
            continue
        node_id = node.get("id")
        if not isinstance(node_id, str) or not node_id:
            report.error(where, "节点缺少字符串 id")
            continue
        if node_id in by_id:
            report.error(where, f"节点 id 重复：{node_id}")
        by_id[node_id] = node
        data_block = node.get("data")
        if not isinstance(data_block, dict):
            report.error(f"节点 {node_id}", "缺少 data")
            continue
        node_type = data_block.get("type")
        if node_type not in NODE_OUTPUTS:
            report.error(f"节点 {node_id}", f"未知节点类型：{node_type!r}")
            continue
        checker = NODE_CHECKERS.get(node_type)
        if checker:
            checker(f"节点 {node_id}({node_type})", data_block, report)
        if node_type == "http-request":
            check_json_body_template(node_id, data_block, env_vars, report)
        if node_type == "code":
            outputs[node_id] = set((data_block.get("outputs") or {}).keys())
        elif node_type == "start":
            outputs[node_id] = {v.get("variable") for v in data_block.get("variables") or [] if isinstance(v, dict)}
        else:
            outputs[node_id] = set(NODE_OUTPUTS.get(node_type, set()))

        if mode == "advanced-chat" and node_type in CHATFLOW_FORBIDDEN:
            report.error(f"节点 {node_id}", f"advanced-chat 不允许 {node_type} 节点，请用 answer 输出")
        if node_type == "start" and index != 0:
            report.warn(f"节点 {node_id}", "start 节点建议放在 nodes[0]")

    edges = graph.get("edges")
    if not isinstance(edges, list) or not edges:
        report.error("graph", "edges 必须是非空列表")
        edges = []
    seen_edges = set()
    for index, edge in enumerate(edges):
        where = f"edges[{index}]"
        if not isinstance(edge, dict):
            report.error(where, "边必须是对象")
            continue
        edge_id = edge.get("id")
        if edge_id in seen_edges:
            report.error(where, f"边 id 重复：{edge_id}")
        seen_edges.add(edge_id)
        for end in ("source", "target"):
            if edge.get(end) not in by_id:
                report.error(where, f"{end}={edge.get(end)!r} 不是已存在的节点")
        if edge.get("sourceHandle", "source") not in {"source", "fail-branch"}:
            report.error(where, f"sourceHandle 非法：{edge.get('sourceHandle')!r}")
        if edge.get("targetHandle", "target") != "target":
            report.warn(where, f"targetHandle 通常是 'target'，当前为 {edge.get('targetHandle')!r}")

    # 引用检查
    for node_id, node in by_id.items():
        data_block = node.get("data") or {}
        for path, text in iter_strings(data_block):
            for head, name in TEMPLATE_RE.findall(text):
                if head in ROOT_NODE_IDS:
                    if head == "env" and name not in env_vars:
                        report.error(f"节点 {node_id}", f"{path} 引用了未定义的环境变量 env.{name}")
                    if head == "sys" and name not in SYSTEM_VARIABLES:
                        report.warn(f"节点 {node_id}", f"{path} 引用了不常见的系统变量 sys.{name}")
                    continue
                if head not in by_id:
                    report.error(f"节点 {node_id}", f"{path} 引用了不存在的节点 {head!r}")
                    continue
                known = outputs.get(head, set())
                if known and name not in known:
                    report.error(
                        f"节点 {node_id}",
                        f"{path} 引用了 {head}.{name}，但 {head} 只产出 {sorted(known)}",
                    )

    if strict:
        report.info("strict 模式：尝试用 graphon 官方模型校验节点 data ...")
        try:
            from graphon.entities.graph_config import NodeConfigDictAdapter  # type: ignore
            from graphon.nodes.answer.entities import AnswerNodeData  # type: ignore
            from graphon.nodes.code.entities import CodeNodeData  # type: ignore
            from graphon.nodes.http_request.entities import HttpRequestNodeData  # type: ignore
            from graphon.nodes.llm.entities import LLMNodeData  # type: ignore
            from graphon.nodes.start.entities import StartNodeData  # type: ignore

            models = {
                "start": StartNodeData,
                "http-request": HttpRequestNodeData,
                "llm": LLMNodeData,
                "code": CodeNodeData,
                "answer": AnswerNodeData,
            }
            for node_id, node in by_id.items():
                data_block = (node.get("data") or {}).copy()
                data_block.setdefault("variables", [])
                if data_block.get("type") == "http-request":
                    data_block.setdefault("body", {"type": "none", "data": []})
                model = models.get(data_block.get("type"))
                if model is None:
                    continue
                try:
                    # NodeConfigDictAdapter 会把 data 解析成 BaseNodeData，这里要的是原始映射
                    NodeConfigDictAdapter.validate_python({"id": node_id, "data": data_block})
                    model.model_validate(data_block)
                except Exception as error:  # noqa: BLE001 - 报告即可
                    report.error(f"节点 {node_id}", f"graphon 模型校验失败：{error}")
            report.info("strict 模式：graphon 校验结束")
        except Exception as error:  # noqa: BLE001
            report.warn("strict", f"graphon 不可用，跳过严格校验（{type(error).__name__}: {error}）")

    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="离线校验 Dify DSL")
    parser.add_argument("dsl", type=Path, help="DSL 文件路径")
    parser.add_argument("--strict", action="store_true", help="用 graphon 官方模型再校验一次")
    args = parser.parse_args()

    data = yaml.safe_load(args.dsl.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        print("[ERROR] DSL 顶层必须是映射")
        return 1
    report = validate(data, strict=args.strict)
    report.info(f"文件：{args.dsl}")
    report.info(f"应用：{data.get('app', {}).get('name')} / 模式：{data.get('app', {}).get('mode')}")
    return report.dump()


if __name__ == "__main__":
    sys.exit(main())
