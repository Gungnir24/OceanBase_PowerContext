# seekdb 的单 SQL 混合候选查询

配置 `SeekDBConfig` 并传入 embedding model 时，内置 Runtime 使用 `SeekDBMemoryHybridIndex`。它继承已有组合索引的初始化、投影写入、删除、恢复及向量复用流程，沿用 `pc_memory_entry_heads`、`pc_memory_entry_versions` 和 `pc_memory_vector_entries`，不新增 schema 或依赖。

`mode="hybrid"` 使用传入的 `AsyncConnection` 执行一条候选 statement。两个带独立 `ORDER BY`、`LIMIT` 的派生表通过 `UNION ALL` 返回全文和向量候选：

- 全文侧继续使用 `MATCH(searchable_text) AGAINST(...)` 及原有得分排序。
- 向量侧继续使用 `l2_distance(...) APPROXIMATE`，保留向量索引查询形式。
- 两侧均在子查询内应用 scope、Memory artifact、标签过滤和候选上限。
- 最外层按通道、通道得分及身份排序，返回的两路候选继续交给现有准入和 RRF 融合逻辑。

查询参数通过 SQLAlchemy 绑定，包含展开的 artifact ID / tag 列表和具有正确维度的 `VECTOR` 参数。标签沿用已有的 scope、family、artifact、target、键及键摘要条件，支持 `all` / `any`，不在 Python 端补做过滤。全文未命中的向量候选也可以返回，混合检索不会变成两路交集。

保持原后端的搜索前后 head 校验及向量完整性检查。不使用投影行的 `head_revision` 与当前 head 作相等过滤：未修改的活跃投影可以留有较旧的修订号。`memory_channel_hits` 仍使用后端已验证的请求 head 构造引用，正文来自同 scope、artifact 和 entry version 的精确 join。

`fts` 和 `vector` 模式委托给原始索引实现；OceanBase 与 SQLite 的组合方式保持原状。追加、正文修订、停用和恢复继续使用原有原子投影更新。

## 验证范围

Windows 本地检查使用记录型连接验证单次候选 execute、SQLAlchemy/MySQL 编译及绑定、双通道顺序和引用、标签参数、错误传播，以及单通道委托。该检查没有运行 seekdb，不能作为两类原生索引实际使用、逐查询质量或延迟的证明。

真实验收需要 Linux x86_64、Python 3.12 和官方锁定的 `pylibseekdb==1.3.0.post4`。按 `competition/README.md` 准备环境后，只运行本题：

```bash
make -C competition test-hybrid
```

本地连接边界测试：

```bash
python -m pytest -q tests/builtin/persistence/test_seekdb_hybrid_query.py
```

原生公开测试会执行实际 `EXPLAIN` 并检查 `TEXT RETRIEVAL SCAN` 和 `VECTOR INDEX`。正式评测还需验证 173 篇文档上的逐查询 Recall@10 / MRR@10 不退化、60 次平均召回以及完整 `Memory.search` 的 p50 不超过 500 ms；这些原生指标尚未在 Windows 环境测量。

SQL 语法参考：[SELECT 与 APPROXIMATE](https://docs.oceanbase.com/oceanbase-database/V4.4.2-select-mysql-compatible-mode)、[UNION](https://docs.oceanbase.com/oceanbase-database/V4.4.2-union-clause-mysql-compatible-mode)。具体固定版本的优化器计划以原生验收为准。
