# 来源依赖的停用与恢复

`open_builtin_runtime()` 提供 `runtime.lifecycle.for_scope(scope_id)`。每个 scope 的依赖图、原始引用、来源生命周期版本和最后接受的事件持久化到同一数据库的 `pc_source_lifecycle` 表；运行期间不使用模块级状态。新表在正常数据库初始化时创建，现有正文和引用不迁移。

```python
from powercontext.builtin.runtime.source_lifecycle import SourceLifecycleEvent

service = runtime.lifecycle.for_scope(scope_id)
await service.register_source("source-a", source_ref, revision=3)
await service.register_memory(
    "fact-a", citation, parents=("source-a",), policy="all"
)
await service.register_topic(
    "topic-a", artifact_ref, parents=("fact-a",), policy="any"
)
active = await service.apply(SourceLifecycleEvent(
    node_id="source-a", revision=3, sequence=1,
    available=False, reason="来源撤回",
))
historical = await service.resolve("fact-a", purpose="audit")
```

注册时检查引用存在并可读取。Source 必须已经捕获，Memory 使用精确 `MemoryCitation`，Topic 使用 `topic-memory` 的 `ArtifactRef`。节点编号不能重复绑定不同内容；同一绑定重放幂等。父节点须先注册，不能重复或指向自身，派生节点的可用性立即按 `all` 或 `any` 计算。

事件字段严格校验类型，拒绝额外字段和空白身份、理由。`revision` 是注册时指定的生命周期版本；每个来源的 `sequence` 独立递增。同序号同内容可以重放，较旧序号或同序号不同内容失败。`apply()` 返回排序后的全部可用节点编号。

Memory 状态通过原有 `forget` / `reactivate` 写入新目录修订，正文版本不变。列表、FTS、向量搜索和 Context 均使用原生状态。Topic 的可恢复治理状态在 `active` / `deprecated` 之间切换，不新增正文修订；原生 Topic 搜索各通道在融合前过滤不可用项，浏览也只返回可用项。精确历史读取不受此过滤影响。

`generation` 只返回可用节点的原始引用，`audit` 也允许返回停用节点的引用。未知节点、用途、版本、冲突事件及非法依赖抛出 `SourceLifecycleError`。

每个 scope 按题目约定使用单写者。写入时先持久化事件与整张图的目标状态，再将原生状态逐项对齐。传播中断后，同一事件重放会继续对齐；已经提交的 Memory 通过实际状态识别，不重复创建修订。即使中断发生在某项提交之后、调用返回之前，重启重试也适用。每次只读取当前 scope 的小型依赖图，节点规模按题目约定为 16 至 20 个。

本题检查命令：

```bash
make -C competition test-lifecycle
python -m pytest -q tests/test_source_lifecycle.py
```

补充测试覆盖严格字段类型、引用不存在、注册冲突、事件误用、独立 scope、多层跨 Topic 依赖、状态写入前后中断、恢复中断、注册中断及重放后的正文版本和 Memory 修订。
