# 验证记录

本文件记录 **实际执行过** 的验证，以及交给评审在有 Docker 环境复现的 live 流程。两类内容
分开标注，避免把演示数据当成真实集成结果。

## 1. 验证环境

| 项目 | 值 |
| --- | --- |
| 日期 | 2026-10-27 |
| 系统 | Windows 11 |
| Python | 3.12.2（`D:\DispositionFile\Python_3.12.2\python.exe`） |
| 浏览器 | Microsoft Edge（无头模式截图） |
| seekdb | **未启动**：本机无 Docker，外网不可达，拉不到镜像 |
| PowerContext | **未启动**：本机缺少 `fastapi/uvicorn/scalar-fastapi` 等服务端依赖，无法安装 |
| 运行模式 | `--demo`（内存数据），页面明确标注「演示模式」 |

> 因此：**UI、业务流程、接口契约、SQL 生成、RRF 融合、记忆客户端逻辑已验证；真实
> seekdb 与 PowerContext Server 的联通性未在本机验证。** 第 3 节给出评审可一键执行的
> 复现步骤与预期证据。

## 2. 已执行的验证

### 2.1 单元与接口测试

```bash
cd qoder
python -m unittest discover -s tests -v
```

结果：

```
Ran 47 tests in 4.749s
OK
```

覆盖范围：

| 文件 | 验证内容 |
| --- | --- |
| `test_ingest.py` | front matter、标题推断、年份容错、按小节切片、长文切分、ID 稳定性 |
| `test_embeddings.py` | 维度/归一化、确定性、相近文本余弦更高、中文二元组、向量字面量 |
| `test_rrf.py` | RRF 排序、两路重叠优先、`matched_by`、top_k、过滤条件与参数类型 |
| `test_seekdb_store.py` | 混合检索 SQL 参数顺序、`APPROXIMATE` 语句、检索日志写入、无全文索引时 LIKE 退化、行映射 |
| `test_powercontext.py` | scope 创建与复用、remember/search/list/retire、prepare、source、错误映射、JSONL 调用日志（对本地假 Server） |
| `test_generation.py` | 抽取式回答带 `[1]`、无命中提示、周报五段结构 |
| `test_api_demo.py` | demo 服务端到端：health、导入、问答、记忆、周报、日志、文献增删、404 |

其中 `test_seekdb_store.py::test_hybrid_search_parameter_order` 专门锁定了开发中真实出现
过的一个 bug：全文 SQL 的 `AGAINST` 占位符与 WHERE 参数顺序错位，会导致过滤条件“吃掉”
查询词。修复后该用例固定了参数顺序 `(fts, scope, year..., fts, limit)`。

### 2.2 demo 模式端到端

```bash
python app/server.py --demo --port 8790
```

| 请求 | 结果 |
| --- | --- |
| `GET /api/health` | `seekdb.ok=true`、`powercontext.ok=true`、`demo=true` |
| `GET /api/papers` | 4 篇示例材料，24 个切片 |
| `POST /api/ask` | 返回带 `[1]…[6]` 的回答，引用含 `chunk_id` 与 `matched_by`，并召回 4 条记忆 |
| `GET /api/weekly?days=7` | 返回五段式 Markdown，含 `[1]` 证据编号 |
| `POST /api/memories` | 写入成功，`GET` 可列出 |
| `GET /api/logs` | 检索日志与记忆调用记录均有数据 |

页面侧用无头浏览器渲染后检查 DOM，确认「把读过的论文」「最近检索」「本轮用到的记忆」
「证据原文」「科研周报」「关键发现」「下周计划」均出现在渲染结果中，不是空壳页面。

### 2.3 截图

| 文件 | 页面 | 说明 |
| --- | --- | --- |
| `docs/screenshots/overview.png` | 概览 | 数据卡片、最近检索、工作流 |
| `docs/screenshots/library.png` | 文献库 | 4 篇示例材料与切片数 |
| `docs/screenshots/ask.png` | 检索问答 | 带引用回答 + 记忆 + 证据原文 |
| `docs/screenshots/memory.png` | 研究记忆 | 记忆列表与写入表单 |
| `docs/screenshots/weekly.png` | 组会周报 | 生成的周报与本周数据 |
| `docs/screenshots/verify.png` | 技术核验 | 依赖状态、检索日志、调用记录 |

> 截图来自 **demo 模式**，页面顶部有「演示模式」标识；它们证明界面与交互，不证明
> seekdb/PowerContext 联通性。

### 2.4 作品链接（Qoder Sites）

| 检查 | 结果 |
| --- | --- |
| 地址 | <https://researchnest-ha7wrs9jpxe.qoder.website> |
| 匿名访问（不带 Qoder 登录态） | HTTP 200，直接渲染项目主页 |
| 页面内容 | 与 `site/index.html` 一致（首屏、功能、技术分工、验证、运行命令） |
| 图片资源 | `screenshots/overview.png`、`ask.png`、`verify.png` 等均返回 `image/png` |

首次发布时访问权限为「仅受邀用户」，匿名访问返回 401；已在 Qoder Sites 中改为
**Anyone can access / Allow public access** 后复查通过。评委无需登录即可打开。

## 3. 评审复现（live，需 Docker）

### 3.1 启动

```bash
cd qoder
docker compose up -d seekdb
uv tool install --force "powercontext[cli,server]==1.2.0"
powercontext server run            # 另开终端，默认 127.0.0.1:8000
python -m pip install -r requirements.txt
cp .env.example .env               # 默认值即可
python scripts/init_db.py
python scripts/seed.py --with-memory
python app/server.py
```

`init_db.py` 的预期输出：

```
[OK] seekdb 已连接
  version       : 5.x.x
  database      : test
  vector index  : HNSW
  fulltext index: 可用
```

若显示“退化为精确扫描”或“LIKE”，说明索引没建起来，请把 `seekdb.health.warnings`
贴到 issue；功能仍可用，但技术核验里会标记退化。

### 3.2 一键验证

```bash
python scripts/verify_live.py
```

预期（节选）：

```
[1/6] 健康检查 http://127.0.0.1:8787
  PASS  seekdb 已连接（...）
  PASS  PowerContext 已连接（scope=...）
  PASS  全文索引可用（不是 LIKE 退化路径）
...
[3/6] 混合检索问答
  PASS  回答长度 ...
  PASS  返回 N 条带出处引用
  PASS  命中 N 条研究记忆
  首条引用：...（chunk_id=..., 通道=vector+fulltext）
...
结果：PASS（N 项全部通过）
```

### 3.3 手工核对点

| 核对项 | 位置 | 预期 |
| --- | --- | --- |
| 向量检索真的走了索引 | 技术核验 → 检索日志 | 候选数 > 0，模式含 `vector` |
| 全文通道真的命中 | 同上 | 全文候选 > 0；无索引时应为 0 并显示 LIKE 退化 |
| RRF 融合 | 检索日志 `fused_ids` | 与页面证据顺序一致 |
| 记忆真的写入 | PowerContext 调用记录 | `写入记忆 remember` 状态 200/201 |
| 记忆真的召回 | 回答右侧「本轮用到的记忆」 | 含刚写入的文本 |
| 记忆跨重启 | 重启 app 后刷新 | scope 不变，记忆仍在 |
| 周报引用 | 周报预览 | `[n]` 与证据清单一一对应 |

SQL 核对：

```sql
-- 最近 5 次检索的两路候选与融合结果
SELECT id, query_text, mode, latency_ms, vector_ids, fts_ids, fused_ids
FROM retrieval_logs ORDER BY id DESC LIMIT 5;

-- 索引是否存在
SHOW INDEX FROM chunks;
```

```bash
# PowerContext 调用审计（本地文件）
tail -n 20 qoder/.state/powercontext-calls.jsonl
```

### 3.4 复现记录（评审填写）

| 日期 | 执行人 | 结果 | 证据 |
| --- | --- | --- | --- |
|  |  |  |  |

## 4. 未覆盖与风险

1. **live 未跑**：这是当前最大的验证缺口；`verify_live.py` 已把检查点写成断言，评审执行
   即可得到 PASS/FAIL，不需要人工比对。
2. **本地哈希向量**的召回质量未做人工评测；示例问题能命中是因为示例材料与问题共享词面。
3. **并发**未压测：`ThreadingHTTPServer` + 每请求新建 MySQL 连接，适合个人使用；多用户
   需要连接池。
4. **PowerContext 版本差异**：客户端按 1.2.0 的接口实现，若服务端升级导致字段变化，
   `verify_live.py` 会在 health 或 search 步骤失败。
