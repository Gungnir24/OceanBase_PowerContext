# 研栖 ResearchNest

**文献找得到，上下文丢不了。** 面向研究生与科研工作者的科研阅读工作台：文献进
**seekdb** 做混合检索，方向、偏好、约束与进展进 **PowerContext**，回答和周报都带出处、
也带得上你的上下文。

> 参赛分支：`2026-competition-ecosystem`（本目录即 `qoder/`）。示例文献、组会记录与实验
> 数字均为虚构，仅用于演示流程；真实使用效果数据在补充后单独标注来源。

---

## 1. 解决的问题

一个典型的科研周：

- 这周读了 5 篇论文、记了 3 条组会笔记，下周要写周报时**找不到当时那句话在哪**；
- 同一篇论文换个问法就搜不到，关键词搜到了却答非所问；
- 每次换一个会话/助手，都要重新解释“我的方向是多模态 RAG”“实验只有单卡 24GB”；
- 组会材料要从聊天记录、笔记、实验日志里**一条条拼**，重复且容易漏。

研栖把这三件事放到同一条链路里：

1. **统一入库**：论文、课程笔记、组会记录都切成带元数据的片段，写进 seekdb；
2. **混合检索**：向量近邻 + 全文匹配 + 年份/会议/标签过滤，RRF 融合后回答，每条论断
   都能点回原文片段；
3. **记忆复用**：方向、表达偏好、实验约束、本周进展写进 PowerContext，下一次提问或
   生成周报时自动带上，并在页面上显示“本轮用到了哪些记忆”。

## 2. 核心功能

| 功能 | 说明 | 依赖的技术 |
| --- | --- | --- |
| 文献库 | Markdown/文本导入，自动解析 `title / authors / year / venue / tags`，切片后入库 | seekdb |
| 混合检索 | 向量 HNSW 召回 + 全文 `MATCH ... AGAINST` 召回，RRF 融合，结构化条件下推 SQL | seekdb |
| 带引用问答 | 回答里的 `[1] [2]` 对应证据原文、切片 ID、命中通道与 RRF 分数 | seekdb |
| 研究记忆 | 写入/停用研究方向、偏好、约束、决策、进展；每轮问答与周报自动召回 | PowerContext |
| 有界上下文 | 生成前调用 `context/prepare`，把记忆压到字节预算内再交给模型 | PowerContext |
| 组会周报 | 汇总本周行为 + 检索证据 + 进展记忆，生成 Markdown，可一键归档为 Source 与记忆 | 两者 |
| 技术核验 | 页面直接展示 seekdb 检索日志与 PowerContext 调用记录，评审可核对 | 两者 |
| 抽取式降级 | 没有 LLM Key 时使用抽取式模板回答，不编造内容，保证离线可跑 | — |
| 演示模式 | `--demo` 使用内存数据预览完整界面，不依赖数据库 | — |

## 3. 技术架构与分工

```
┌──────────────────────────── 浏览器（原生 JS 单页） ────────────────────────────┐
│  概览 │ 文献库 │ 检索问答 │ 研究记忆 │ 组会周报 │ 技术核验                        │
└───────────────────────────────────┬──────────────────────────────────────────┘
                                    │ JSON /api/*
┌───────────────────────────────────▼──────────────────────────────────────────┐
│  app/server.py（Python 标准库 ThreadingHTTPServer）                            │
│  app/service.py 编排：导入 → 检索 → 记忆 → 生成 → 归档                         │
└───────────────┬───────────────────────────────────┬──────────────────────────┘
                │ pymysql（MySQL 协议）              │ HTTP（urllib）
┌───────────────▼──────────────────┐   ┌────────────▼─────────────────────────┐
│ seekdb                            │   │ PowerContext Server                  │
│ papers / chunks / activity /      │   │ Scope → Memory → PreparedContext     │
│ retrieval_logs                    │   │ + Source 归档                        │
│ VECTOR(384) + HNSW + FULLTEXT(ik) │   │ 调用记录写入本地 JSONL                │
└───────────────────────────────────┘   └──────────────────────────────────────┘
```

**分工说明（评审重点）**

- **seekdb 负责“证据与检索”**：文献正文、切片、向量、全文索引、结构化字段、检索日志都在
  seekdb 里；一次检索会真实执行两条 SQL（向量 `l2_distance ... APPROXIMATE`、全文
  `MATCH ... AGAINST`），融合与应用侧 RRF 完成后，把两路候选与融合结果写入
  `retrieval_logs`。页面「技术核验」展示的就是这张表。
- **PowerContext 负责“跨会话的上下文”**：研究记忆以 Memory 形式持久化；每轮问答先
  `memory/search` 召回相关记忆，再 `context/prepare` 取有界上下文；周报归档时把正文写成
  Source、把摘要写成一条 `status` 记忆。所有调用都追加到
  `.state/powercontext-calls.jsonl`，页面同样可查。
- **两者不是二选一**：seekdb 回答“这句话在哪”，PowerContext 回答“我是谁、做到哪了”。

## 4. 快速开始

### 4.1 环境要求

| 组件 | 版本 / 说明 |
| --- | --- |
| Python | 3.11+（开发验证使用 3.12.2） |
| pymysql | >= 1.1,<2（唯一的 Python 第三方依赖） |
| seekdb | `oceanbase/seekdb:latest`，Docker 24+；MySQL 协议端口 2881 |
| PowerContext | 1.2.0（`uv tool install "powercontext[cli,server]==1.2.0"`） |
| 操作系统 | Linux / macOS 推荐；Windows 可运行应用与 PowerContext，seekdb 建议用 Docker |

### 4.2 启动 seekdb

```bash
cd qoder
docker compose up -d seekdb
# 首次启动需要十几秒；确认端口
docker compose logs -f seekdb
```

如果使用已有的 seekdb 实例，跳过这一步，改 `.env` 里的 `SEEKDB_HOST/PORT/USER/PASSWORD`。

### 4.3 启动 PowerContext

```bash
# 方式一：PyPI（推荐给评审）
uv tool install --force "powercontext[cli,server]==1.2.0"
powercontext server run

# 方式二：从比赛仓库的完整源码安装（仓库包含 src/powercontext 时）
# python -m pip install -e ".[server]"
# powercontext server run
```

默认监听 `http://127.0.0.1:8000`，使用 SQLite 持久化；不需要模型即可保存/检索显式 Memory。
若启用鉴权，把 token 填到 `.env` 的 `POWERCONTEXT_TOKEN`。

### 4.4 配置并启动应用

```bash
cd qoder
python -m pip install -r requirements.txt
cp .env.example .env          # Windows: Copy-Item .env.example .env
# 按需修改 SEEKDB_*、POWERCONTEXT_*

python scripts/init_db.py     # 建表 + 向量/全文索引，并打印索引状态
python scripts/seed.py --with-memory   # 导入 4 篇示例材料 + 4 条示例记忆
python app/server.py          # 或 .\scripts\run.ps1 / ./scripts/run.sh
```

打开 <http://127.0.0.1:8787>。首次启动会自动创建 PowerContext Scope，并写入
`.state/powercontext-scope.json`；之后重启复用同一个 scope。

### 4.5 只想先看界面？

```bash
python app/server.py --demo
```

演示模式使用内存数据，页面顶部会明确标注「演示模式」；它不连接 seekdb / PowerContext，
不能作为技术结合的证据，仅用于预览交互。

## 5. 验证样例（可按步骤执行）

> 目标：证明 seekdb 的检索结果与 PowerContext 的记忆都真实参与了项目流程。
> 全程不需要 LLM Key。

### 步骤 1：入库

```bash
python scripts/seed.py --with-memory
```

预期：输出 4 篇材料与切片数；`python scripts/init_db.py` 显示
`vector index: HNSW`、`fulltext index: 可用`。

### 步骤 2：混合检索问答

页面「检索问答」输入：

> 混合检索里 RRF 融合的 k 值应该怎么选？有实验数据吗？

预期：

- 回答带 `[1] [2]` 引用，正文出现 `Recall@10`、`k=20/60/100` 等来自示例材料的细节；
- 右侧「本轮用到的记忆」出现研究方向、偏好、约束、进展；
- 「检索通道」显示向量候选与全文候选均大于 0，耗时以毫秒计。

**技术作用记录**：进入「技术核验」，`seekdb 检索日志` 最新一行应包含本次查询、
`vector+fts`、融合 Top3 的 chunk_id 与耗时；`PowerContext 调用记录` 应出现
`检索记忆 search` 与 `准备上下文 prepare`。

也可以直接用 SQL 核对：

```sql
SELECT query_text, mode, latency_ms, fused_ids
FROM retrieval_logs ORDER BY id DESC LIMIT 3;
```

### 步骤 3：记忆被复用

1. 在「研究记忆」写入：`实验约束：单卡 24GB，向量维度 384。`（类型选“约束”）；
2. 重新提问：`我的实验能用 1024 维向量吗？`；
3. 预期：回答的「结合你的研究记忆」出现刚写入的约束；「技术核验」新增一条 `写入记忆 remember`。

重启应用（甚至重启 PowerContext Server）后刷新页面，记忆仍存在——这是它和“会话内上
下文”的区别。

### 步骤 4：周报

「组会周报」选择近 7 天 → 生成。预期：出现「本周概览 / 阅读与检索 / 关键发现（带出处）/
进展与阻塞 / 下周计划」，引用编号与证据清单对应。点击「归档到 PowerContext」后：

- 周报正文作为 Source 保存；
- 自动写入一条 `status` 记忆；
- 「技术核验」新增 `保存证据 source` 与 `写入记忆 remember`。

### 步骤 5：一键回归

```bash
python scripts/verify_live.py
```

脚本会对运行中的应用执行上述主流程，逐项打印 PASS/FAIL，并输出首条引用、检索通道、
记忆召回与日志条数。全部 PASS 时退出码为 0。

## 6. 配置项

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `APP_HOST` / `APP_PORT` | `127.0.0.1` / `8787` | Web 监听地址 |
| `APP_DEMO` | `false` | 演示模式（内存数据） |
| `APP_STATE_DIR` | `./.state` | scope 与调用日志目录 |
| `SEEKDB_HOST/PORT` | `127.0.0.1` / `2881` | seekdb 连接 |
| `SEEKDB_USER/PASSWORD` | `root` / 空 | seekdb 账号 |
| `SEEKDB_DATABASE` | `test` | 数据库名，不存在会自动创建 |
| `SEEKDB_FTS_PARSER` | `ik` | 中文全文解析器；失败时回退 `ngram` → 默认 |
| `EMBEDDING_DIM` | `384` | 向量维度，需与 `VECTOR(N)` 一致 |
| `EMBEDDING_PROVIDER` | `local` | `local` 哈希向量 / `openai` 兼容接口 |
| `EMBEDDING_API_KEY` | 空 | 使用 `openai` 时必填 |
| `RETRIEVAL_TOP_K_VECTOR/FTS` | `12` / `12` | 两路候选数 |
| `RETRIEVAL_RRF_K` | `60` | RRF 平滑常数 |
| `POWERCONTEXT_URL` | `http://127.0.0.1:8000` | Server 地址 |
| `POWERCONTEXT_TOKEN` | 空 | 启用鉴权时填写 |
| `POWERCONTEXT_SCOPE_ID` | 空 | 留空自动创建并落盘 |
| `LLM_BASE_URL/MODEL/API_KEY` | 空 | 可选；不配置则走抽取式回答 |

> 本地哈希向量是确定性的词面向量（中文单字+二元组、英文词元），优点是无网络、零成本、
> 结果可复现；局限是召回同义改写不如真实 embedding。配置 `EMBEDDING_PROVIDER=openai`
> 后会改用真实模型，届时需要同步调整 `EMBEDDING_DIM` 并重建表。

## 7. 测试

```bash
cd qoder
python -m unittest discover -s tests -v
```

当前 47 个用例全部通过，覆盖：

- 文档解析/切片/ID 稳定性（`test_ingest.py`）
- 本地向量、归一化、中文分词、向量字面量（`test_embeddings.py`）
- RRF 融合、过滤条件、SQL 参数顺序与退化路径（`test_rrf.py`、`test_seekdb_store.py`）
- PowerContext 客户端：scope 复用、记忆写入/检索/停用、上下文、Source、错误映射、调用日志
  （`test_powercontext.py`，使用本地假 Server）
- 抽取式回答与周报结构（`test_generation.py`）
- demo 模式 API 端到端：健康检查、导入、问答、记忆、周报、日志、增删文献、404
  （`test_api_demo.py`）

## 8. 目录结构

```
qoder/
├── app/
│   ├── server.py          # 标准库 HTTP 服务与路由
│   ├── service.py         # 业务流程编排
│   ├── seekdb_store.py    # 建表、写入、混合检索、检索日志
│   ├── powercontext.py    # HTTP 客户端、scope、记忆、上下文、调用审计
│   ├── generation.py      # LLM / 抽取式回答与周报
│   ├── ingest.py          # front matter、切片、ID
│   ├── embeddings.py      # 本地哈希向量 / OpenAI 兼容向量
│   ├── demo.py            # 演示模式的内存实现
│   ├── config.py          # 环境变量与 .env
│   └── static/            # 原生 JS 单页
├── data/samples/          # 虚构示例文献与笔记
├── scripts/               # init_db / seed / verify_live / run
├── tests/                 # 47 个测试
├── docs/                  # 架构、验证记录、Qoder 开发记录、截图
├── site/                  # Qoder Sites 发布用静态站
├── docker-compose.yml     # seekdb
├── requirements.txt
└── .env.example
```

## 9. 已知限制

- **本机未跑过 live 链路**：开发环境没有 Docker，且外网不可达，seekdb 与 PowerContext
  Server 无法启动；因此本仓库的 live 路径是通过单元测试、假 PowerContext 服务与 demo
  API 端到端测试验证的。`scripts/verify_live.py` 是给评审在有 Docker 的环境下一键复现用的。
- 本地哈希向量只捕捉词面相近，同义改写召回有限；生产建议换真实 embedding。
- 目前是单用户/单 scope 设计；多用户需要按用户分配 scope，并在网关上做鉴权。
- PDF 未直接解析，需先转成 Markdown/文本；表格行作为独立证据单元尚未实现。
- Windows 下 seekdb 嵌入式后端不可用，请用 Docker 或远程实例。

## 10. 提交材料与开发记录

- 代码仓库：`https://github.com/Gungnir24/OceanBase_PowerContext`（分支 `2026-competition-ecosystem`）
- 作品链接：<https://researchnest-ha7wrs9jpxe.qoder.website>
- 运行说明：本文第 4、5 节
- 验证样例：本文第 5 节 + `scripts/verify_live.py`
- Qoder 开发记录：`docs/qoder-development-log.md`
- 架构说明：`docs/architecture.md`
- 页面截图：`docs/screenshots/`
- 项目网站源码：`site/index.html`（已通过 Qoder Sites 发布）

## 11. 数据与开源声明

- `data/samples/` 下的论文、组会记录、实验数字均为虚构，仅用于演示；不包含任何真实用户
  数据、姓名、联系方式或密钥。
- PowerContext 与 seekdb 的使用遵循其各自开源许可；本目录只包含应用代码。
- `.env`、`.state/`、`.data/` 已在 `.gitignore` 中排除，提交前请确认没有真实凭据。
