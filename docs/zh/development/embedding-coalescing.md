# Embedding 并发合并与向量缓存

`CoalescingEmbeddingModel` 是显式启用的模型包装器。原有模型可以继续直接使用；需要共享请求时，让同一事件循环中的调用方持有同一个包装器实例。

```python
from powercontext.builtin.inference.coalescing import CoalescingEmbeddingModel

model = CoalescingEmbeddingModel(
    base_model,
    max_batch_size=64,
    coalesce_ms=5.0,
    cache_size=1024,
)
result = await model.embed(("first", "second", "first"))
```

包装器暴露底层 `profile`，返回原有 `EmbeddingResult`，不新增依赖。文本按原值比较，不去空格、不转换大小写。返回向量保留调用输入的顺序和重复位置，空输入不调用底层模型。

## 合并、批次和缓存

新文本先进入短暂的合并窗口，再按 `max_batch_size` 分批。`coalesce_ms=0` 会在事件循环的调度边界派发工作。窗口内的重复文本和已经执行中的相同文本共用结果。

每个底层请求的去重文本数不超过上限。不同批次独立执行，单个批次失败不会阻止其他批次完成。向量数量与请求文本数不一致时，该批次以 `ValueError` 失败，不缓存部分结果。

成功向量进入容量有界的 LRU。缓存命中及成功消费会刷新使用顺序。`cache_size=0` 关闭跨调用缓存，但仍共享未完成的请求。底层异常不进入缓存，后续调用可重试。

## 取消和 usage

调用方仅等待受保护的共享 Future。取消一个调用不会取消共享底层任务，也不会取消其他等待者。即使所有等待者都取消，底层正常完成的向量仍可进入缓存，失败也不会产生未回收的 Future 异常。

每个调用只汇总自己等待过的不同底层批次，各批次按 provider 返回的 `requests`、`input_tokens` 和 `output_tokens` 统计。同批次涉及多个文本或重复位置也只计一次；不同等待者可以分别计入同一共享批次。纯缓存命中的 `requests` 为 0。若任一依赖批次未报告某个 token 字段，聚合结果的该字段保持未知 `None`。

对于实现 `embed_query` 的非对称 provider，查询与文档使用不同的缓存和合并键，并调用各自接口；对于仅提供 `embed` 的对称 provider，查询复用文档缓存。

## 原生接入

```python
async with open_builtin_contexts(config, embedding_model=model) as contexts:
    service = (await contexts.get(scope_id)).artifacts.memory
    # 继续使用已有 Memory 写入及向量检索接口。
```

公开原生请求预算使用六个 scope、24 条共享文本、`max_batch_size=6`、`coalesce_ms=0`、`cache_size=48`，要求底层恰好四次请求，后续缓存搜索不新增请求。

## 验证

仅运行本题公开测试和补充边界测试：

```bash
make -C competition test-embedding
python -m pytest -q tests/builtin/inference/test_coalescing.py
```

补充测试覆盖晚到等待者、关闭缓存、重复位置、LRU 刷新、不同批次的 usage、失败重试、独立批次成功、向量数量错误、取消后的异常回收以及查询/文档方向隔离。
