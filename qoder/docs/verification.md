# 验证记录

本文件记录**实际执行过**的验证，所有输出都来自真实 seekdb（Docker）与真实 PowerContext
Server，可直接按第 2 节的命令复现。

> 上一版提交时本机没有 Docker，live 链路只写成"评审可复现步骤"，也因此漏掉了三个只在
> 真实数据库上才会暴露的 bug（详见第 5 节）。这次全部在真库上跑通并修复。

## 1. 验证环境

| 项目 | 值 |
| --- | --- |
| 日期 | 2026-10-27 |
| 系统 | Windows 11（宿主机）+ Docker Desktop |
| Python | 3.14.6（宿主机；项目要求 3.11+） |
| 依赖 | `pymysql`；PowerContext 1.2.0（`powercontext[cli,server]`） |
| seekdb | 官方镜像 `oceanbase/seekdb:latest`，容器内版本 `5.7.25-OceanBase seekdb-v1.4.0.0`，MySQL 协议 `127.0.0.1:2881` |
| PowerContext | 1.2.0，`http://127.0.0.1:8000`，SQLite 持久化 |
| 向量/全文 | `VECTOR(384)` + HNSW（`DISTANCE=L2, LIB=VSAG`）+ `FULLTEXT ... WITH PARSER ik` |

索引确实建成了（`SHOW INDEX FROM chunks`）：

```
VECTOR KEY   idx_chunks_vec (embedding) WITH (DISTANCE=L2, TYPE=HNSW, LIB=VSAG, M=16, ...)
FULLTEXT KEY idx_chunks_fts (content)   WITH PARSER ik PARSER_PROPERTIES=(ik_mode="smart")
```

## 2. 从零复现（评审可直接照做）

```bash
# 1) 起 seekdb
docker compose up -d seekdb          # 或 docker run -p 2881:2881 -p 2886:2886 oceanbase/seekdb:latest

# 2) 起 PowerContext（另开终端）
uv tool install --force "powercontext[cli,server]==1.2.0"
powercontext server run              # 默认 127.0.0.1:8000

# 3) 起应用
cd qoder
python -m pip install -r requirements.txt
cp .env.example .env                 # 默认值即可
python scripts/init_db.py            # 建表 + 向量/全文索引 + 表结构自检
python scripts/seed.py --with-memory # 4 篇示例材料 + 5 条研究记忆
python app/server.py                 # http://127.0.0.1:8787

# 4) 一键验收
python scripts/verify_live.py        # 全部 PASS 时退出码为 0
```

`init_db.py` 实际输出：

```
[OK] seekdb 已连接
  version       : 5.7.25-OceanBase seekdb-v1.4.0.0
  database      : test
  vector index  : HNSW
  fulltext index: 可用
```

> **Windows 提示**：若 PowerShell 下 PowerContext 报
> `attempt to write a readonly database` 或 `PermissionError ... cursor-key`，是默认数据目录
> （`%LOCALAPPDATA%\PowerContext`）在该机器上不可写。设置 `POWERCONTEXT_HOME` 指向可写目录
> 即可，例如 `$env:POWERCONTEXT_HOME="D:\pc-home"`，再启动 Server。

## 3. 一键验收输出（`scripts/verify_live.py`）

```
[1/6] 健康检查 http://127.0.0.1:8787
  PASS  seekdb 已连接（5.7.25-OceanBase seekdb-v1.4.0.0）
  PASS  PowerContext 已连接（scope=scp_7qrqb84xpxz443d9mr3vwef08t）
  PASS  全文索引可用（不是 LIKE 退化路径）
  PASS  向量索引可用（ANN 未退化为精确扫描）

[2/6] 文献库
  PASS  文献库有 4 篇材料

[3/6] 混合检索问答
  PASS  回答长度 892 字
  PASS  返回 6 条带出处引用
  PASS  检索通道无报错（两条 SQL 都成功执行）
  PASS  每条引用都带非空的文献标题
  PASS  两路召回均有候选（向量 12 / 全文 12）
  PASS  命中 1 条研究记忆
  首条引用：《面向中文长文档的混合检索：向量、全文与结构化过滤的协同》 · 片段 1
            （chunk_id=372df5ed513bf2e9-000，通道=vector+fulltext）
  检索通道：向量候选 12，全文候选 12，耗时 65 ms

[4/6] 记忆写入与复用
  PASS  记忆已写入并可列出
  PASS  新问题能召回刚写入的记忆

[5/6] 组会周报
  PASS  周报包含标题
  PASS  周报包含带出处的关键发现
  PASS  周报引用 6 条证据

[6/6] 技术核验日志
  PASS  seekdb 检索日志 9 条
  PASS  PowerContext 调用记录 10 条

============================================================
结果：PASS（18 项全部通过）
```

## 4. SQL 级证据（技术结合怎么核对）

### 4.1 向量通道：应用真实生成的 SQL + 直接执行结果

```sql
SELECT id, paper_id, title, section, chunk_index, content, year, venue, tags,
       l2_distance(embedding, [0.000000, ..., 0.000000]) AS distance   -- 384 维字面量已截断
FROM chunks WHERE scope_id = %s
ORDER BY distance APPROXIMATE LIMIT %s
```

在 seekdb 上执行（scope 取自 `retrieval_logs`）：

```
distance=1.1574  title=面向中文长文档的混合检索：向量、全文与结构化过滤的协同  chunk=372df5ed513bf2e9-006
distance=1.2610  title=组会记录 · 2026 年 10 月第 4 周                        chunk=7be764ae801ca0a5-001
distance=1.2687  title=面向中文长文档的混合检索：向量、全文与结构化过滤的协同  chunk=372df5ed513bf2e9-000
```

### 4.2 全文通道：应用真实生成的 SQL + 直接执行结果

```sql
SELECT id, paper_id, title, section, chunk_index, content, year, venue, tags,
       MATCH(content) AGAINST(%s IN BOOLEAN MODE) AS score
FROM chunks
WHERE scope_id = %s AND MATCH(content) AGAINST(%s IN BOOLEAN MODE)
ORDER BY score DESC LIMIT %s
```

查询词由 `tokens_for_query` 生成（中文单字/二元组 + 英文词元，截断 24 个）：

```
混合 合检 检索 索里 融合 合的 值应 应该 该怎 怎么 么选 有实 实验 验数 数据 据吗 rrf k
```

执行结果：

```
score=3.3507  title=面向中文长文档的混合检索：向量、全文与结构化过滤的协同  chunk=372df5ed513bf2e9-000
score=2.8801  title=组会记录 · 2026 年 10 月第 4 周                        chunk=7be764ae801ca0a5-001
score=2.6164  title=Agent 长期记忆的写入、检索与遗忘：一个可审计的上下文服...  chunk=2acd970e3d75b115-000
```

### 4.3 两路融合与检索日志

RRF（k=60）融合后 Top3：

```
1. rrf=0.03227  channel=vector+fulltext  chunk=372df5ed513bf2e9-000
2. rrf=0.03226  channel=vector+fulltext  chunk=7be764ae801ca0a5-001
3. rrf=0.03202  channel=vector+fulltext  chunk=372df5ed513bf2e9-006
```

`retrieval_logs` 最新一行（页面「技术核验」展示的就是这张表）：

```
id=9  mode=vector+fts  latency_ms=38
query_text  = 混合检索里 RRF 融合的 k 值应该怎么选？有实验数据吗？
vector_ids  = ["372df5ed513bf2e9-006", "7be764ae801ca0a5-001", ...]（12 条）
fts_ids     = ["372df5ed513bf2e9-000", "7be764ae801ca0a5-001", ...]（12 条）
fused_ids   = [{"id":"372df5ed513bf2e9-000","rrf":0.03227,"matched_by":["vector","fulltext"]}, ...]
note        = （空：两条通道都没有报错）
```

### 4.4 PowerContext 侧

- `.state/powercontext-calls.jsonl`：`resolve_scope → remember ×5 → search → prepare` 逐条记录
  状态码与耗时；页面「技术核验」同源展示。
- 记忆写入后重启应用、重启 PowerContext Server，刷新页面记忆仍在（scope 复用，见
  `.state/powercontext-scope.json`）。
- 记忆召回：`POST /v1/memory/search` 命中后进入回答的「本轮用到的记忆」，并随
  `context/prepare` 的有界上下文一起交给生成器。

## 5. 本次修复的问题（提测反馈）

提测结论是"检索 SQL 引用不存在的 `title` 字段，问答无法返回证据"。在真库复现后确认，
这只是最表层的一个，live 链路上共三处必现问题：

| # | 现象 | 根因 | 修复 | 验证方式 |
| --- | --- | --- | --- | --- |
| 1 | `(1054, "Unknown column 'title' in 'field list'")`，两路候选全 0，问答只剩"没有检索到" | `chunks` 表没有 `title` 列，但向量/全文 SQL 都 `SELECT title` | `chunks` 增加 `title`，写入时带 `document.title`；老库启动时 `ALTER TABLE` 补列并从 `papers` 回填 | 4.1/4.2 的 SQL 直接执行有结果；`verify_live.py` 断言"每条引用都带非空标题" |
| 2 | `TypeError: tuple indices must be integers`（health）、`AttributeError: 'tuple' object has no attribute 'get'`（文献列表） | `_execute` 用默认 cursor，返回 tuple，全代码却按列名取值 | 连接统一使用 `pymysql.cursors.DictCursor` | 健康检查、文献库、问答、日志、周报全部 200 |
| 3 | 第二次启动报"向量索引创建失败，退化为精确扫描"，但索引其实存在 | 重复建索引时 seekdb 返回 `1235 ... has vector index is not supported`，代码只识别 `exists`/`Duplicate` | 建索引前用 `SHOW INDEX` 预检，已存在直接置为可用 | `health.seekdb.vector_index=true`，`verify_live.py` 新增该断言 |
| 附 | live 链路缺一层"表结构对不对"的守护 | 47 个单测全部 mock 掉 `_execute`，SQL 与 DDL 从未对账 | 新增 `tests/test_schema_contract.py`（不需要数据库）+ `tests/test_seekdb_live.py`（`SEEKDB_LIVE=1` 时跑真库）+ 启动时 `_verify_schema()` 快速失败 | `python -m unittest discover -s tests`：54 个用例全通过 |
| 附 | 从 `qoder/` 目录启动 PowerContext 直接退出，报 `Invalid default environment file: invalid assignment at ...\.env:42` | `.env.example` 里 `POWERCONTEXT_SCOPE_TITLE=研栖 · 科研工作台` 未加引号，值里既有空格又有 `·`，dotenv 解析失败 | 该值改成带引号，应用侧解析本来就会去引号，行为不变 | PowerContext 在 `qoder/` 目录下正常启动，健康检查通过 |
| 附 | 中文短问句召不回记忆时，"本轮用到的记忆"为空 | 记忆检索是词面召回，提问措辞与记忆无字面交集就 0 命中 | 命中为空时按常驻主题（研究方向/偏好/约束/进展）再召回一次，并在页面标注"按常驻主题召回" | `verify_live.py` 的"命中 1 条研究记忆" |

## 6. 截图

`docs/screenshots/` 下的 `overview / library / ask / memory / weekly / verify` 为 live 模式
（页面顶部**没有**「演示模式」标识）渲染结果，对应上表同一套数据。

## 7. 未覆盖与风险

1. **本地哈希向量**只捕捉词面相近，同义改写召回有限；换成真实 embedding 需同步调整
   `EMBEDDING_DIM` 并重建表。
2. **记忆召回的词面依赖**：PowerContext 1.2.0 在未配置 embedding 模型时按全文召回，
   中文短问句可能 0 命中，因此应用加了常驻主题兜底；配置模型后可关闭该兜底。
3. **并发未压测**：`ThreadingHTTPServer` + 每请求新建 MySQL 连接，适合个人使用；多用户需要
   连接池与按用户分配 scope。
4. **PDF 未直接解析**，表格行也还不是独立证据单元。
5. `seekdb` 中文全文解析器用 `ik`；若实例不支持，会依次回退 `ngram` → 默认解析器，并退化为
   `LIKE` 匹配（`/api/health` 的 `fts_available` 会为 false，页面会标明退化）。

## 8. 复现记录（评审填写）

| 日期 | 执行人 | 结果 | 证据 |
| --- | --- | --- | --- |
| 2026-10-27 | 作者本机（Windows 11 + Docker + PowerContext 1.2.0） | `verify_live.py` 18/18 PASS | 本文件第 3、4 节 |
|  |  |  |  |
