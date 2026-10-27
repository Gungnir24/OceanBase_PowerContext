# 架构说明

本文记录研栖 ResearchNest 的技术选择、数据模型与关键流程，便于评审对照代码。

## 1. 设计取舍

| 决策 | 选择 | 原因 |
| --- | --- | --- |
| 后端框架 | Python 标准库 `ThreadingHTTPServer` | 依赖越少越容易复现；评审只需 `pymysql` 一个包 |
| 前端 | 原生 JS + 手写 Markdown 渲染 | 无 Node 构建链，页面可直接静态托管 |
| 文献存储 | seekdb（MySQL 协议） | 向量、全文、结构化字段在同一张表，过滤可以下推 SQL |
| 上下文 | PowerContext HTTP API | 跨会话记忆、有界上下文、Source 归档是它原生能力 |
| 向量 | 默认本地哈希向量，可切 OpenAI 兼容接口 | 无网络也能跑通全链路；换真实模型只需改配置 |
| 生成 | 有 Key 用 LLM，无 Key 用抽取式 | 不让“没有模型”变成“打不开页面”，且抽取式不编造 |
| ID | 文件名 + 标题哈希 / 论文 ID + 序号 | 重复导入幂等，切片可定位 |

## 2. 组件

```
浏览器
  └─ app/static/{index.html, app.js, styles.css}
       └─ /api/*  ── app/server.py ── app/service.py
                                        ├─ app/ingest.py      解析与切片
                                        ├─ app/embeddings.py  向量
                                        ├─ app/seekdb_store.py
                                        ├─ app/powercontext.py
                                        └─ app/generation.py
```

`AppService` 是唯一编排层，HTTP 层只做参数校验与 JSON 序列化；演示模式把
`SeekDBStore` / `PowerContextClient` 替换为 `app/demo.py` 的内存实现，其余代码不变。

## 3. 数据模型（seekdb）

```sql
papers(
  id VARCHAR(64) PK, scope_id, title, authors, year, venue, tags,
  source_name, content LONGTEXT, created_at
)

chunks(
  id VARCHAR(64) PK, paper_id, scope_id, chunk_index, section,
  title VARCHAR(512), content TEXT, embedding VECTOR(384), year, venue, tags, created_at,
  VECTOR INDEX idx_chunks_vec (embedding)
    WITH (DISTANCE=l2, TYPE=hnsw, LIB=vsag),
  FULLTEXT INDEX idx_chunks_fts (content) WITH PARSER ik
)

activity(id BIGINT PK AI, scope_id, kind, ref_id, title, detail, created_at)

retrieval_logs(
  id BIGINT PK AI, scope_id, query_text, filters_json,
  vector_ids, fts_ids, fused_ids, mode, latency_ms, note, created_at
)
```

设计要点：

- `chunks` 冗余 `year/venue/tags`，检索时无需 join `papers` 就能下推过滤；
- `chunks` 同时冗余 `title`：回答里的引用必须显示文献标题，而 ANN 检索要保持单表
  `ORDER BY distance APPROXIMATE`（join 会让近似检索的代价与可读性都变差）。老库启动时
  会自动 `ALTER TABLE` 补列并从 `papers` 回填，不需要重建库；
- `embedding` 与 `content` 同表，向量与全文两路 SQL 命中同一批行，融合成本低；
- 索引创建做了三级回退：`vsag` → 无 LIB 的 HNSW → 精确扫描；全文 `ik` → `ngram` →
  默认解析器。失败原因写入 `warnings`，页面「技术核验」可见；
- `retrieval_logs` 是可审计性的核心：两路候选 ID、融合结果、模式、耗时都落库。

## 4. 写入流程

1. `parse_document` 解析 front matter（`title/authors/year/venue/tags`），没有则用一级标题；
2. `chunk_text` 按 `##` 小节切分，单块不超过 520 字、重叠 80 字；过短段落并入上一块，
   但**不跨小节合并**，保证 `section` 语义正确；
3. `LocalHashEmbedder` 或远程 embedding 生成 384 维向量并 L2 归一化；
4. 同一 `paper_id` 先删旧切片再插入，重复导入幂等；
5. 写一条 `activity(kind='ingest')`，供周报统计。

## 5. 检索流程

```
query ──┬─ 向量 SQL：ORDER BY l2_distance(embedding, vec) APPROXIMATE LIMIT 12
        └─ 全文 SQL：MATCH(content) AGAINST(q IN BOOLEAN MODE) LIMIT 12
                     （无全文索引时退化为 content LIKE）
                         │
                    RRF 融合：score(d) = Σ 1 / (k + rank_i(d))，k=60
                         │
                 结构化过滤已下推 WHERE：scope_id / year / venue / tags
                         │
                 写 retrieval_logs（两路 ID、融合 ID、耗时）
                         │
                 取前 top_k 交给生成层，附 citation
```

- 两路各自排名从 1 开始，同一片段在向量与全文都出现时分数相加，因此“两路都命中”的
  片段会排到最前；
- `matched_by` 记录命中通道，页面用彩色标签展示；
- 查询词做了布尔模式清洗（去掉 `+ - " * ( ) ~ < >`），避免用户输入触发语法错误；
- 过滤参数做了类型校验，`"2020"` 这类字符串不会被拼进 SQL。

## 6. 记忆生命周期

```
启动 ──> resolve_scope
           ├─ 有 POWERCONTEXT_SCOPE_ID → 直接用
           ├─ 有 .state/powercontext-scope.json → 复用
           └─ 都没有 → POST /v1/scopes，写状态文件

写入：POST /v1/memory/remember {kind, text, reason}
检索：POST /v1/memory/search {query, limit}
停用：POST /v1/memory/entries/retire {citation, reason}
准备：POST /v1/context/prepare {task, max_bytes}
归档：POST /v1/sources/content（周报正文）
      + remember(kind='status', text=摘要)
```

每次调用都追加 `.state/powercontext-calls.jsonl`（时间、操作、参数摘要、状态码、耗时），
页面「技术核验」读取最近 20 条展示。记忆的权威存储在 PowerContext Server 侧，应用重启
不会丢失。

## 7. 生成与降级

- `Generator.answer`：有 LLM 时把证据编号后交给模型，要求“只依据证据、必须带 [n]”；
  无 LLM 时选与问题词重叠最高的句子，按 `[n]` 拼接，并明确写出“抽取式摘要”；
- 记忆会以“结合你的研究记忆”段落加入回答，来源是 PowerContext 召回结果；
- `weekly_report`：统计行为、检索查询、命中文献与进展记忆，按固定五段输出；LLM 可用时
  只改写措辞，不改变引用编号与事实。

## 8. 失败与降级矩阵

| 故障 | 行为 |
| --- | --- |
| seekdb 连不上 | 启动时 health 显示未连接，页面顶部红色提示；live 接口返回 503 错误信封 |
| 向量索引创建失败 | 退化为精确扫描，`warnings` 说明原因，检索仍可用 |
| 全文索引/解析器不可用 | 退化为 `LIKE` 关键词通道，`fts_available=false` |
| PowerContext 不在线 | 问答与周报仍可用，只是没有记忆；`memory_warning` 提示 |
| 记忆写入超过 8192 字节 | 客户端截断并提示，不阻塞主流程 |
| LLM 超时/报错 | 回退抽取式回答，并在 `warning` 中说明 |
| 重复导入同一文件 | 按 `paper_id` 覆盖，切片不重复 |

## 9. HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 依赖状态、scope、向量维度、LLM 是否启用 |
| GET | `/api/papers` | 文献列表（含切片数） |
| POST | `/api/papers` | `{name, content}` 导入 |
| GET | `/api/papers/{id}` | 文献详情 + 切片 |
| DELETE | `/api/papers/{id}` | 删除文献与切片 |
| POST | `/api/samples/import` | 导入内置示例 |
| POST | `/api/search` | 只检索不生成 |
| POST | `/api/ask` | `{question, filters, top_k}` 检索 + 记忆 + 回答 |
| GET/POST | `/api/memories` | 列出 / 写入记忆 |
| POST | `/api/memories/retire` | 停用记忆 |
| GET | `/api/weekly?days=&focus=` | 生成周报 |
| POST | `/api/weekly/archive` | 归档周报为 Source + 记忆 |
| GET | `/api/logs?limit=` | 检索日志 + PowerContext 调用 |
| GET | `/api/stats` | 概览计数 |

错误统一为 `{"error": {"code", "message"}}`，HTTP 状态码区分 400/404/409/503。

## 10. 可验证性

- **seekdb 侧**：`retrieval_logs` 可直接 SQL 查询；页面展示融合 Top3 的 chunk_id；
- **PowerContext 侧**：调用日志 JSONL + 页面表格；记忆条目带 `entry_id` 与版本号；
- **一致性**：回答里的 `[n]` 与证据列表、周报引用使用同一套 `citations` 结构，编号不会错位。

## 11. 安全与隐私

- 所有密钥只从环境变量/.env 读取，`.env` 已 gitignore；
- 示例数据为虚构，不含真实姓名与联系方式；
- 页面输出统一 HTML 转义；Markdown 渲染只支持受控子集，不执行脚本；
- 单用户单 scope 模型；多用户部署需在上层加鉴权并按用户隔离 scope。

## 12. 后续演进

1. PDF 解析与表格行级证据；
2. 真实 embedding 与维度自适应（重建索引脚本）；
3. 记忆去重/合并与冲突检测（同一约束的新旧版本）；
4. 多用户 scope 与团队共享文献库；
5. 周报按项目/课题分组，支持导出 docx。
