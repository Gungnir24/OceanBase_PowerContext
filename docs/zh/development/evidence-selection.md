# 查询驱动的原文证据选择

`PrepareContextRequest.evidence_selection` 是默认关闭的严格布尔开关。开启时，`PreparedContextBuilder.build_scopes_result()` 只从传入的 Memory 候选中选取原文；关闭时继续使用已有组装和前缀截取行为。

```python
request = PrepareContextRequest(
    query="cobalt sensor calibration",
    max_bytes=1400,
    evidence_selection=True,
)
result = PreparedContextBuilder().build_scopes_result(
    request=request,
    current_scope_id=scope_id,
    memory_candidates=candidates,
)
for fragment in result.fragments:
    # address 包含 scope、Memory revision、entry ID 和 entry version ID。
    print(fragment.address, fragment.start, fragment.end, fragment.text)
```

选择器遍历候选的完整正文，按句界保留原字符坐标。离线评分结合查询词频、候选内词项稀有度、文档覆盖率和长度归一化，并轻量处理英文词尾及中文二元词。相邻句组合提供上下文，同一条目中分离的证据可以独立输出。单句本身超出预算时，使用查询位置附近的原文窗口。所有片段都由 `entry.text[start:end]` 直接截取，不对输出正文做归一化、摘要或翻译。

候选选项的成本使用实际序列化字节数。选择阶段同时计算 trust policy、envelope、完整 citation、JSON 转义、字符坐标、正文和片段间逗号的 UTF-8 开销，以有界的成本/得分前沿选择可装入的组合。最后再次核对完整输出大小。无可用片段或引用开销已经超过预算时返回空 Context。

非空输出沿用 `TRUST_POLICY`，使用 `BEGIN_POWERCONTEXT_PREPARED_CONTEXT_V1` / `END_POWERCONTEXT_PREPARED_CONTEXT_V1`，中间是单行 JSON。每项含 `citation`、`content`、`start` 和 `end`；特殊 Unicode 换行字符在 JSON 中转义，解析后仍是原文。`result.origins` 与 `result.fragments` 逐项对应，所有引用始终带 scope，不以相同正文替代身份。预算探针使用同一选择路径。

实现只依赖 Python 标准库及项目现有类型，不访问文件、网络、模型、标注或判题资产。测试中的 Runtime 真实保存 Memory，并在关闭、重启后按精确引用回读片段。

仅运行本题测试：

```bash
make -C competition test-evidence
python -m pytest -q tests/test_evidence_selection.py
```

公开测试检查功能，未提供正式质量标注。公开测试通过不能证明隐藏 48 次请求已经达到 evidence token recall 0.65 和完整证据比例 40% 的质量门槛。
