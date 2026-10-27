# Qoder 开发记录

> 本文件记录研栖 ResearchNest 的需求拆解、关键提示词、迭代与调试过程，作为“开发过程”的
> 说明。**文末的 Qoder 客户端截图与账号 UID 需要参赛者本人在 Qoder 中补齐**：仓库不包含
> 伪造的截图或 UID，这一节目前是待填状态，提交前请按 6.2 的清单完成。

## 1. 项目信息

| 项 | 值 |
| --- | --- |
| 项目名 | 研栖 ResearchNest |
| 一句话 | 文献找得到，上下文丢不了 |
| 参赛分支 | `2026-competition-ecosystem` |
| 代码目录 | `qoder/` |
| 核心技术 | seekdb（向量 + 全文 + 结构化过滤）、PowerContext（跨会话记忆 + 有界上下文） |
| 开发周期 | 2026-10（需求拆解 → 骨架 → 检索 → 记忆 → 前端 → 测试 → 文档） |

## 2. 需求拆解

先把“科研阅读”拆成可验证的四个动作，再决定技术归属：

| 动作 | 用户痛点 | 技术归属 | 验收方式 |
| --- | --- | --- | --- |
| 存 | 论文、笔记散落在文件夹与聊天记录 | seekdb：`papers` / `chunks` | 导入后切片数、元数据正确 |
| 找 | 关键词搜到但不相关，换个问法搜不到 | seekdb：向量 + 全文 + RRF + 过滤 | 两路候选均 > 0，引用可回原文 |
| 记住 | 每个会话都要重新解释方向与约束 | PowerContext：Memory + prepare | 重启后记忆仍在，问答能召回 |
| 交付 | 周报靠手工拼，容易漏 | 两者：检索证据 + 进展记忆 | 五段式周报，引用编号对齐 |

明确不做：PDF 解析、多人协作、模型训练。把范围压到“一个人一周”的真实闭环。

## 3. 迭代记录

| 轮次 | 目标 | 产出 | 验收 / 返工 |
| --- | --- | --- | --- |
| 1 | 跑通 seekdb 建表与写入 | `seekdb_store.py` 四张表、向量/全文索引三级回退 | 索引语句在无 `vsag` 时必须能降级 |
| 2 | 混合检索 | 向量 SQL、全文 SQL、RRF、过滤下推、`retrieval_logs` | 发现全文参数顺序错位，补测试锁定 |
| 3 | 接入 PowerContext | scope 自动创建/复用、remember/search/retire/prepare/source、JSONL 审计 | scope 不能每次启动都新建 |
| 4 | 编排与页面 | `service.py`、`server.py`、单页前端六个视图 | demo 模式必须与 live 同一套接口 |
| 5 | 可验证性 | 技术核验页、`verify_live.py`、54 个测试 | 评审不读代码也能核对技术作用 |
| 6 | 文档与提交 | README、架构、验证记录、截图、Sites 草稿 | 运行说明按步骤可复制 |

## 4. 关键提示词（可在 Qoder 中复用）

以下是实际驱动开发的任务描述，按顺序投喂即可复现主体结构：

1. **建骨架**
   > 用 Python 标准库写一个科研阅读工作台，目录 `qoder/app`。配置从环境变量和 `.env`
   > 读取；不要引入 Web 框架，HTTP 用 `ThreadingHTTPServer`。先给出模块划分和每个文件的
   > 职责，再写代码。
2. **seekdb 存储**
   > 用 pymysql 连接 seekdb（MySQL 协议）。建 `papers`、`chunks`、`activity`、
   > `retrieval_logs` 四张表；`chunks.embedding` 用 `VECTOR(384)`，建 HNSW 向量索引和
   > `WITH PARSER ik` 全文索引；索引创建失败要逐级降级并记录 warnings，不能直接崩。
3. **混合检索**
   > `search(scope_id, query, vector, filters, top_k)`：向量近邻与全文 `MATCH ... AGAINST`
   > 各取 Top-K，用 RRF(k=60) 融合；年份/会议/标签条件下推到 SQL；返回两路候选数、融合
   > 结果、耗时，并写入 `retrieval_logs`。注意 `AGAINST` 出现在 SELECT 和 WHERE 两处，
   > 参数顺序要写对。
4. **PowerContext 客户端**
   > 封装 `resolve_scope / remember / search / list_entries / retire / prepare /
   > capture_source`。scope 优先用环境变量，其次读 `.state/powercontext-scope.json`，
   > 都没有才创建；每次调用追加 JSONL 审计（操作、参数摘要、状态码、耗时）；错误转成
   > 带 status 的异常。
5. **编排与降级**
   > `AppService` 负责导入、问答、周报、记忆。没有 LLM Key 时用抽取式回答，必须带 `[n]`
   > 引用且不编造；PowerContext 不可用时问答仍要能返回，只提示“没有记忆”。
6. **前端**
   > 原生 JS 单页，六个视图：概览、文献库、检索问答、研究记忆、组会周报、技术核验。
   > 不要构建工具；回答里的 `[n]` 要能对应证据列表；技术核验页直接展示检索日志和
   > PowerContext 调用记录。
7. **测试**
   > 用 unittest，不依赖 pytest。覆盖切片、向量、RRF、SQL 参数顺序、PowerContext 客户端
   > （起一个本地假 Server）、抽取式回答、demo API 端到端。测试不能依赖 Docker。

## 5. 调试记录（真实发生的问题）

| 现象 | 根因 | 修复 |
| --- | --- | --- |
| `/api/ask` 500：`'<' not supported between dict and dict` | demo 搜索对 `(score, hit)` 排序时取了 `item[1]` | 改为按 `item[0]` 排序 |
| 带年份过滤时查询词被当成年份 | 全文 SQL 两处 `AGAINST` 与 WHERE 参数顺序错位 | 固定为 `(fts, scope, filters..., fts, limit)`，加单测 |
| demo 启动报 `PermissionError: qoder\.state` | 演示模式仍构造真实 PowerContext 客户端并写状态目录 | demo 模式改用内存客户端；live 支持 `APP_STATE_DIR` |
| 小节标题串到上一段 | 过短段落跨小节合并 | 合并时要求 `section` 相同 |
| 无 Docker 时页面打不开 | live 依赖 seekdb | 增加 `--demo`，并保证接口契约一致 |
| 提测：检索 SQL 引用不存在的 `title`，问答无证据 | `chunks` 表没有该列；另有两个被 demo/单测掩盖的问题 | 见第 8 节（真库复现后修复） |

这些返工都保留在测试里，避免回归。

## 6. Qoder 使用证据（提交前补齐）

### 6.1 当前状态

- 代码、测试、文档已完成，项目网站已通过 Qoder Sites 发布（链接见 6.3）；
- Qoder 账号 UID 已记录；
- **开发过程截图（`qoder-01` ~ `qoder-05`）尚未补充**。

### 6.2 需要补的截图（建议 5 张）

| 文件名 | 画面内容 | 目的 |
| --- | --- | --- |
| `qoder-01-requirement.png` | Qoder 对话中粘贴赛题与需求拆解 | 证明需求分析过程 |
| `qoder-02-codegen.png` | 生成 `seekdb_store.py` 混合检索的对话与 diff | 证明核心代码由 Qoder 辅助 |
| `qoder-03-debug.png` | 定位 SQL 参数顺序 / demo 权限问题的对话 | 证明调试过程 |
| `qoder-04-run.png` | Qoder 终端里启动 demo 或 live、测试通过 | 证明可运行 |
| `qoder-05-sites.png` | Qoder Sites 发布页面与 URL | 证明作品已上线 |

要求：

- 截图里要能看到 **Qoder 客户端界面与账号 UID**（通常在左下角头像/设置-账号）；
- 不要把 token、邮箱、手机号截进去；
- 图片放 `docs/screenshots/`，在本文件 6.3 表格中登记。

### 6.3 证据登记

| 项 | 值 |
| --- | --- |
| Qoder 账号 UID | `01a17c79-8c6d-7847-a400-ff950ce4d89d` |
| 使用起止时间 | 2026-10-26 ~ 2026-10-27 |
| 截图 | `待补充`（qoder-01 ~ qoder-05） |
| Qoder Sites 作品链接 | <https://researchnest-ha7wrs9jpxe.qoder.website> |

> 说明：以上三项是赛事硬性材料，必须由参赛者本人提供；代码仓库不会伪造。

## 7. 复盘

- **做对的**：先定“存/找/记/交付”四个动作，再决定 seekdb 与 PowerContext 的分工，避免
  为了用技术而用技术；把“可核验”做成页面和脚本，而不是只写在文档里。
- **不足**：首版提交前没有真库可跑，live 链路只写成可复现步骤——这也直接导致三个必现
  bug 被带进了提交（详见第 8 节）。单元测试把 `_execute` 全部 mock 掉，等于从未验证过
  "SQL 里的列在表里是否存在"。
- **改进**：这次补了表结构契约测试与真库集成测试，并把 live 验证做成一键脚本；下一步补 Qoder 截图与 UID。

## 8. 提测问题修复（2026-10-27，真库复现）

### 8.1 提测结论

> 检索 SQL 引用不存在的 `title` 字段，问答无法返回证据。

首版是在"没有 Docker、没跑过 live"的前提下提交的，所以这条反馈不是偶发问题，而是
live 链路的必现故障。拿到 Docker 后按 README 起 `oceanbase/seekdb:latest`
（`seekdb-v1.4.0.0`）复现，发现真实原因不止一个：

### 8.2 根因与修复

| # | 复现到的现象 | 根因 | 修复 |
| --- | --- | --- | --- |
| 1 | `(1054, "Unknown column 'title' in 'field list'")`；向量与全文两条 SQL 都失败，候选为 0，问答只剩"没有检索到" | 建表时 `chunks` 没有 `title` 列，但两条检索 SQL 都 `SELECT title`，`_hit_from_row` 也按 `row["title"]` 取值 | `chunks` 增加 `title VARCHAR(512)`；写入时带 `document.title`；老库启动时查 `information_schema` 补列，并从 `papers` 回填 |
| 2 | `TypeError: tuple indices must be integers`（`/api/health`）、`AttributeError: 'tuple' object has no attribute 'get'`（文献列表） | `_execute` 用默认 cursor，返回 tuple，但全代码按列名访问 | 连接统一 `cursorclass=pymysql.cursors.DictCursor` |
| 3 | 第二次启动报"向量索引创建失败，退化为精确扫描"，但 `SHOW INDEX` 显示索引存在 | 重复建索引时 seekdb 返回 `1235 ... has vector index is not supported`，代码只识别 `exists`/`Duplicate` | 建索引前用 `SHOW INDEX` 预检，已存在即视为可用 |
| 4 | 中文短问句召不回记忆时，"本轮用到的记忆"为空 | 记忆检索是按词面召回，问句与记忆没有字面交集就 0 命中 | 命中为空时按常驻主题（研究方向/偏好/约束/进展）再召回一次，页面标注"按常驻主题召回" |
| 5 | 服务端数据被清空后，应用一直 404 `Scope was not found` | `.state` 里缓存了 scope_id，重启后不校验服务端是否还有这个 scope | `resolve_scope` 先用轻量读接口确认 scope 存在，404 则重建 |

### 8.3 为什么单测全绿却漏了

47 个用例把 `_execute` 整个 mock 掉，等于只验证了"SQL 字符串拼得对"，从未验证
"SQL 里的列在表里存在""返回的行能按列名取值"。补了两层守护：

| 新增 | 作用 |
| --- | --- |
| `tests/test_schema_contract.py` | 从建表语句解析列名，断言检索 SQL 引用的列都存在（不需要数据库，这次事故的直接防线） |
| `tests/test_seekdb_live.py` | `SEEKDB_LIVE=1` 时对真实 seekdb 跑建表→导入→混合检索→检索日志，断言两路候选 > 0 且标题非空 |
| `SeekDBStore._verify_schema()` | 启动时 `SELECT <检索依赖列> ... LIMIT 0`，缺列直接抛错，不再静默降级 |
| `verify_live.py` 新增 3 条断言 | 向量索引可用、每条引用标题非空、两路候选均 > 0 |

### 8.4 验证结果

```
python -m unittest discover -s tests          # Ran 54 tests ... OK (skipped=3)
SEEKDB_LIVE=1 python -m unittest discover -s tests   # Ran 54 tests ... OK
python scripts/verify_live.py                 # PASS（18 项全部通过）
```

检索实测：向量候选 12 / 全文候选 12 / 融合耗时 54–102 ms / 6 条带出处引用，
`retrieval_logs` 的 `note` 为空（说明两条通道都没有报错）。完整输出见
`docs/verification.md` 第 3、4 节。

### 8.5 这一轮的 Qoder 使用建议（截图用）

建议在 Qoder 里用一句话把问题交给它，保留"复现 → 定位 → 改 → 验证"的完整往返：

> 提测反馈说检索 SQL 引用了不存在的 title 字段、问答没有证据。请先在本机起
> seekdb（Docker）复现，贴出真实报错，再逐个定位根因并修复；修完补一个不依赖数据库的
> 表结构契约测试和一个真库集成测试，最后跑 verify_live.py 给出 PASS 输出。

对应截图建议命名为 `qoder-06-debug-live.png`（复现与定位）与 `qoder-07-verify-live.png`
（54 个测试 + 18 项 PASS），登记到 6.3 表格。
