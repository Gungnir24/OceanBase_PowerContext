# 规有所依 · SDD 上下文管理系统（MiniMax 赛道）

面向持续演进项目的工程上下文模块：给定一个开发任务，从 **seekdb** 检索出**当前生效**的规格条目、
需要修改的代码符号与验证用测试，并把「已完成 / 待办 / 已有验证结果」从 **PowerContext** 带回来；
上下文交给 **MiniMax** 改代码，改完用同一组测试验证。

本目录（`minimax/`）就是完整交付物：可运行的上下文模块、被修改的业务工程、MiniMax 开发记录、
数据库与 PowerContext 的使用记录、以及一键验收脚本。

- 参赛分支：`2026-competition-ecosystem`
- 业务工程：`workdir/`（`catalog` 商品目录检索服务，纯标准库 + pytest）
- 上下文模块：`sddctx/`（CLI + 可编程 API）
- 验证样例： [`docs/01-验证样例.md`](docs/01-验证样例.md)
- 技术说明（一页）： [`docs/02-技术说明.md`](docs/02-技术说明.md)
- MiniMax 开发记录： [`docs/03-MiniMax开发记录.md`](docs/03-MiniMax开发记录.md)
- seekdb 查询记录： [`docs/04-数据库查询记录.md`](docs/04-数据库查询记录.md)
- PowerContext 记录： [`docs/05-PowerContext状态记录.md`](docs/05-PowerContext状态记录.md)

---

## 0. 六项必做项与证据位置

| 验收项 | 本作品的做法 | 证据 |
| --- | --- | --- |
| 能够运行 | 纯 Python，seekdb 用 docker compose 起；`verify_all.py` 从空库跑通全流程 | README 第 5 节、`docs/logs/acceptance-*.log` |
| MiniMax 实际参与 | MiniMax 读取上下文 + 源文件全文，输出整文件补丁；2 轮内通过 31 个测试 | `docs/03-MiniMax开发记录.md`、`docs/minimax/*round*-response.json`、`data/patches/` |
| 上下文正确 | 输出 15 条规格（含 FILTER-001..004、COMPAT-001..004、ADR-0011）、18 处代码、31 个测试；`ADR-0007` 作为失效决策被显式排除 | `docs/01-验证样例.md`、`docs/context-sample.json` |
| 数据库实际参与 | 规格条目 / 代码符号 / 测试 / 检索审计全部存 seekdb；一次装配执行 4 条检索 SQL，命中明细入库 | `docs/04-数据库查询记录.md`、`retrieval_logs` 表 |
| 状态能够复用 | 任务进展与验证结果写入 PowerContext；新进程通过 `memory/entries/list` 读回并参与上下文 | `docs/05-PowerContext状态记录.md`、`tests/test_state_reuse.py` |
| 代码修改有效 | 修改前 14 个目标测试失败、14 个回归测试通过；修改后 31 个全通过（测试文件未改动） | `docs/logs/*baseline*`、`docs/logs/*after-change*` |

---

## 1. 目录结构

```text
minimax/
├── sddctx/                 # 上下文模块（CLI + 可编程 API）
│   ├── cli.py              # python -m sddctx 的入口
│   ├── ingest.py           # 规格/代码/测试 -> seekdb（增量、幂等）
│   ├── retrieve.py         # 全文(ngram) + 向量(l2) + RRF 融合
│   ├── assemble.py         # 上下文装配与预算裁剪、Markdown 渲染
│   ├── state.py            # 任务状态读写（PowerContext 为主）
│   ├── powercontext.py     # PowerContext REST 客户端 + 调用留痕
│   ├── minimax_client.py   # MiniMax（OpenAI 兼容）客户端
│   ├── specs.py            # 规格条目解析
│   ├── codeindex.py        # AST 符号/片段抽取
│   └── db.py               # seekdb 连接与建表
├── workdir/                # 被修改的业务工程
│   ├── biz/catalog/        #   实现（models/store/filters/service/api/server/errors）
│   ├── tests/              #   目标测试 17 个 + 回归测试 14 个
│   └── data/products.json  #   种子数据（20 个商品）
├── data/
│   ├── specs/              # SDD 规格：接口契约、SRS、ADR、测试计划
│   ├── baseline/           # 修改前代码快照（含 MANIFEST）
│   └── patches/            # MiniMax 产生的统一 diff
├── scripts/                # init_db / demo_context / minimax_change / replay_* / verify_all
├── tests/                  # 上下文模块自身的测试（12 个）
├── docs/                   # 交付文档、MiniMax 记录、日志
└── docker-compose.yml      # seekdb
```

---

## 2. 依赖与版本

| 组件 | 版本 / 说明 |
| --- | --- |
| Python | 开发与验证使用 **3.14.6**；模块要求 **>= 3.11**（用了 `X \| None` 类型语法） |
| PyMySQL | **2.2.8**（`requirements.txt`：`>=1.1,<3`），唯一第三方运行时依赖 |
| pytest | **9.0.3** |
| seekdb | 镜像 `oceanbase/seekdb:latest`，实测服务端 `5.7.25-OceanBase seekdb-v1.4.0.0`，MySQL 协议端口 2881 |
| PowerContext | Server **1.2.0**（PyPI 包 `powercontext`），REST `/v1`，默认 `127.0.0.1:8000` |
| MiniMax | 模型 **`minimax/minimax-m2.5`**，通过 OpenAI 兼容网关 `POST /chat/completions` 调用 |

接入方式一览（题目要求「注明实际采用的方式和版本」）：

| 技术 | 实际接入方式 | 承担的作用 |
| --- | --- | --- |
| MiniMax | HTTP（OpenAI 兼容 Chat Completions），标准库 `urllib` 直连，无 SDK | 读上下文生成业务代码补丁 |
| seekdb | MySQL 协议（PyMySQL），`VECTOR(384)` + `FULLTEXT ... WITH PARSER ngram` | 存工程资料/索引，执行检索，记录检索审计 |
| PowerContext | HTTP REST `/v1`（Scope / Memory / context/prepare / sources/content） | 保存任务进展与验证结果，重启后读回 |

---

## 3. 配置

```bash
cp .env.example .env
```

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SEEKDB_HOST` / `SEEKDB_PORT` | `127.0.0.1` / `2881` | seekdb 地址 |
| `SEEKDB_USER` / `SEEKDB_PASSWORD` | `root` / 空 | 演示用无密码 |
| `SEEKDB_DATABASE` | `sddctx` | 模块会自动 `CREATE DATABASE IF NOT EXISTS` |
| `POWERCONTEXT_URL` | `http://127.0.0.1:8000` | PowerContext Server |
| `POWERCONTEXT_SCOPE_ID` | 空 | 留空则自动创建并写入 `.state/powercontext-scope.json` |
| `MINIMAX_BASE_URL` | `https://api.ppio.com/openai/v1` | 任意 OpenAI 兼容网关均可 |
| `MINIMAX_API_KEY` | 空 | **必填**；不填也能运行除 MiniMax 之外的步骤 |
| `MINIMAX_MODEL` | `minimax/minimax-m2.5` | 模型 ID |

`.env` 已被 `.gitignore` 忽略；仓库里只有一个占位值的 `.env.example`，不含任何真实凭据。

> 说明：MiniMax 官方与多家聚合网关都提供 OpenAI 兼容接口。本作品实测使用的是
> `https://api.ppio.com/openai/v1` + `minimax/minimax-m2.5`；若要换成 MiniMax 官方端点，
> 只需改 `MINIMAX_BASE_URL` / `MINIMAX_API_KEY` / `MINIMAX_MODEL` 三个变量，代码无需改动。

---

## 4. 启动依赖服务

### 4.1 seekdb

```bash
docker compose -f docker-compose.yml up -d
# 首次启动约 10~30 秒；端口 2881
```

如果本机已有 seekdb（OceanBase）实例，跳过这一步，直接改 `.env` 的 `SEEKDB_*`。

### 4.2 PowerContext Server

```bash
pip install "powercontext[cli,server]==1.2.0"
powercontext server run --host 127.0.0.1 --port 8000
```

服务端数据默认落在它自己的本地存储中；本模块只通过 REST 访问，不依赖其内部结构。

---

## 5. 初始化数据

```bash
pip install -r requirements.txt

python scripts/init_db.py          # 等价于 python -m sddctx import --reset
python -m sddctx doctor            # 自检：seekdb / PowerContext / MiniMax Key
```

`init_db.py` 会：建库建表 → 解析 `data/specs/*.md` 成条目 → 用 AST 切分 `workdir/**/*.py` →
写入 `documents / clauses / chunks / symbols`。它是幂等的：内容没变就跳过（`skipped_unchanged`）。

预期输出（节选）：

```json
{"project": "catalog", "documents": 16, "clauses": 16, "chunks": 105, "symbols": 78, ...}
```

---

## 6. 调用入口与输入输出

### 6.1 CLI（主要入口）

```bash
python -m sddctx <command> [options]
```

| 命令 | 输入 | 输出 |
| --- | --- | --- |
| `doctor` | — | 各依赖连通性 JSON，`ok: true/false` |
| `import [--reset] [--force]` | `data/specs/`、`workdir/` | 导入统计 JSON（文档/条目/片段/符号数） |
| `context --task "..." [--task-id T] [--json] [--max-bytes N]` | 任务描述（必填）、任务 ID（可选） | Markdown 上下文；`--json` 时为结构化 JSON |
| `state init/add/show` | 任务 ID、标题、条目内容 | 写入/读回结果，含 PowerContext `entry_id` |
| `verify --task-id T --label L -- <命令>` | 任意测试命令 | 执行日志文件 + 退出码，并把结果写入 PowerContext |
| `db stats` / `db sql --sql "..."` | — | 库内存量统计 / 任意查询结果 |

`context` 的 JSON 结构（`schema = sddctx.context/v1`）：

```json
{
  "task": {"task_id": "T-2026-1028", "text": "...", "endpoint": "/api/products"},
  "specs": [{"clause_id": "FILTER-001", "title": "价格区间筛选",
             "doc_path": "data/specs/API-catalog-v2.md", "doc_version": "2.3.0",
             "status": "active", "applies_to": "GET /api/products",
             "selected_by": ["适用面", "全文/向量"], "body": "...",
             "code_refs": ["workdir/biz/catalog/service.py#search_products"],
             "test_refs": ["workdir/tests/test_search_target.py#test_price_max_only"]}],
  "excluded_specs": [{"clause_id": "ADR-0007", "status": "superseded", "replaced_by": "ADR-0011"}],
  "code": [{"path": "workdir/biz/catalog/service.py", "symbol": "search_products",
            "start_line": 57, "end_line": 92, "source": "anchor", "clauses": ["FILTER-001"],
            "excerpt": "..."}],
  "tests": [{"path": "workdir/tests/test_search_target.py", "test_id": "test_price_max_only",
             "kind": "target", "source": "anchor"}],
  "state": {"task_id": "T-2026-1028", "done": ["..."], "todo": ["..."], "validations": [{"exit_code": 0}]},
  "retrieval": {"run_id": 5, "latency_ms": 197, "hit_channels": {"fts": 21, "vector": 36}},
  "diagnostics": {"missing_refs": [], "truncated": false, "rendered_bytes": 24305}
}
```

### 6.2 可编程 API

```python
from sddctx import load_config, open_db, build_context, ingest

cfg = load_config()
db = open_db(cfg.seekdb)
ingest(cfg, db)                      # 幂等
context = build_context(db=db, project=cfg.project, task_text="给 /api/products 加价格筛选")
print(context["markdown"])
```

### 6.3 Agent 接入

任何 Agent 只要能把 `context["markdown"]` 塞进提示词即可；本作品里的接入实现是
`scripts/minimax_change.py`（见 `docs/03-MiniMax开发记录.md`）。

---

## 7. 跑验证样例

```bash
# 1) 看上下文（会带上 PowerContext 里的任务状态）
python scripts/demo_context.py --task-id T-2026-1028

# 2) 修改前：回滚基线并跑测试（目标测试应当失败）
python scripts/replay_baseline.py

# 3) 修改后：重放 MiniMax 记录的补丁并跑测试（应当 31 passed）
python scripts/replay_minimax.py --check

# 4) 规格更新后重新导入，上下文随之变化
python scripts/spec_update_demo.py
```

完整步骤、预期结果与重置方法见 [`docs/01-验证样例.md`](docs/01-验证样例.md)。

---

## 8. 让 MiniMax 重新完成一次修改

```bash
export MINIMAX_API_KEY=<your-token>       # Windows PowerShell: $env:MINIMAX_API_KEY="..."
python scripts/minimax_change.py --task-id T-2026-1028 --apply
```

脚本会：装配上下文 → 调用 MiniMax（多轮，失败回滚基线后带 pytest 输出重试）→ 全量测试 →
写 `docs/minimax/<时间戳>-round*-response.json` 与 `dev-log.json` → 生成 `data/patches/*.patch`
→ 重新索引代码 → 把完成事项写回 PowerContext。

**不复现模型随机性**：评审不需要重跑模型，用 `scripts/replay_minimax.py --check` 即可验证
「提交的代码 == 当时 MiniMax 的输出」，它比对的是 `docs/minimax/applied-digests.json` 里的 sha256。

---

## 9. 测试命令与结果

```bash
# 业务工程：目标 + 回归（31 个）
python -m pytest workdir/tests -q
# 上下文模块自身（12 个；需要 seekdb + PowerContext，缺服务会自动 skip）
python -m pytest tests -q
# 一键验收（六项必做项逐条检查，写 docs/logs/acceptance-*.log）
python scripts/verify_all.py
```

本机实测（Python 3.14.6 / seekdb v1.4.0.0）：

| 场景 | 命令 | 结果 |
| --- | --- | --- |
| 修改前（目标测试） | `pytest workdir/tests/test_search_target.py -q` | `14 failed, 3 passed`，exit 1 |
| 修改前（回归测试） | `pytest workdir/tests/test_search_regression.py -q` | `14 passed`，exit 0 |
| 修改后（全量） | `pytest workdir/tests -q` | `31 passed`，exit 0 |
| 上下文模块 | `pytest tests -q` | `12 passed` |
| 一键验收 | `python scripts/verify_all.py` | 17/17 通过 |

日志在 `docs/logs/`，每条都记录了命令、退出码与代码版本。

---

## 10. 已知限制

1. **向量检索是暴力扫描**：seekdb 1.4.0 不支持在 `VECTOR` 列上建索引（`create index on vector ...`
   报 `1235`），所以 `l2_distance` 走全表。片段量在万级以内可接受，十万级需要换 ANN 方案。
2. **内置 embedding 是确定性哈希向量**（`sddctx/embeddings.py`），只保证「词面相近」，
   不做语义泛化；需要更强语义召回时可把 `embed()` 换成远端 embedding，接口不变。
3. **MiniMax 输出格式是整文件 JSON**，不是标准 patch：这样更稳，但要求目标文件已存在
   （脚本会拒绝新建文件与修改测试）。
4. **PowerContext 用的是单机默认存储**，未做多副本/鉴权；生产环境应开启 token 与 HTTPS。
5. **Windows 上 git 可能不在 PATH**：脚本已做兜底查找，日志里 `code_version` 可能显示
   `unknown (git 不在 PATH)`，不影响测试结论。
6. 演示数据为虚构商品目录，仅用于验证流程，不涉及真实业务数据。

---

## 11. 复用与第三方说明

- 业务工程 `workdir/biz/catalog` 与规格文档均为本作品原创，未复制第三方代码。
- 依赖的开源组件：PyMySQL（MIT）、pytest（MIT）、seekdb（Apache-2.0）、PowerContext（见其仓库 LICENSE）。
- 模型调用：MiniMax `minimax/minimax-m2.5`，经 OpenAI 兼容网关；请求/响应原文保留在 `docs/minimax/`。

## 12. 凭据与隐私

仓库内不含任何真实 API Key、Token 或云端凭据；`.env` 被忽略，`.env.example` 只有占位值。
`docs/minimax/` 保留的是模型输出正文与 token 用量，不含模型私有思维链。
