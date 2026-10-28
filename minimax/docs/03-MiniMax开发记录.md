# MiniMax 开发记录

本记录对应一次**真实发生**的代码修改：MiniMax 读工程上下文 + 源文件全文，输出整文件补丁，
工作区落盘后跑测试，2 轮内从 `1 failed` 修到 `31 passed`。所有原始请求/响应都在 `docs/minimax/`。

---

## 1. 接入方式与版本

| 项 | 值 |
| --- | --- |
| 模型 | `minimax/minimax-m2.5` |
| 协议 | OpenAI 兼容 `POST {base}/chat/completions`（非官方 SDK，标准库 `urllib` 直连） |
| 端点 | `https://api.ppio.com/openai/v1`（OpenAI 兼容聚合网关；换官方端点只需改环境变量） |
| 采样参数 | `temperature=0.2`、`max_tokens=8000`、请求超时 300 s |
| 调用实现 | `sddctx/minimax_client.py`（`MiniMaxClient.chat / chat_json`） |
| 驱动脚本 | `scripts/minimax_change.py` |
| 请求/响应留痕 | `docs/minimax/20261028-135014-round1-request.json`、`*-round1-response.json`、`*-round2-response.json` |
| Agent 决策轨迹 | `docs/minimax/20261028-135014-dev-log.json` |
| 产生的 diff | `data/patches/0001-price-and-stock-filter.patch` |

API Key 只从环境变量 `MINIMAX_API_KEY` 读取，仓库内不含任何真实凭据。

## 2. 给了 MiniMax 什么

`scripts/minimax_change.py` 先用上下文模块装配 `T-2026-1028` 的上下文，再拼成一次请求（共 42,092 字符）：

| 内容 | 说明 |
| --- | --- |
| 任务描述 | 价格区间 + 库存筛选 + 400/`INVALID_FILTER` + 组合语义 + 兼容性要求 |
| 适用规格 15 条 | FILTER-001..004、COMPAT-001..004、ADR-0011、SRS-F-014..016、TEST-001..003（含条目正文与声明的关联代码/测试） |
| 失效决策 | ADR-0007 被标注为「已取代 → ADR-0011」，提示不要实现旧参数名 |
| 需要修改的源文件全文 | `workdir/biz/catalog/{service,api,filters}.py` |
| 验收测试全文 | `workdir/tests/test_search_{target,regression}.py` + `conftest.py`，明确「禁止修改，用于理解期望行为」 |
| 任务状态 | PowerContext 中的已完成/待办/历史验证结果 |

系统提示词的硬约束（原文见 `*-round1-request.json`）：

```text
- 只能修改 workdir/biz/ 下已存在的文件；禁止新建文件、禁止修改 workdir/tests/（含 conftest.py）；
- files 里的 content 必须是完整文件内容（不是 diff、不是片段）；
- 只使用 Python 标准库，不新增第三方依赖；
- 保持公开函数签名向后兼容；错误必须用 catalog.errors.ApiError 抛出；
- 规格里标为「已取代 / 已废弃」的决策一律不得实现；
- 测试文件与 conftest.py 是验收口径：即使测试失败也不得修改它们。
```

> 为什么先把测试给模型？因为题目要求「同时提供验证该修改的测试用例」，测试就是验收口径，
> 隐藏它反而会让模型猜测边界。防「对着测试写死」靠三点：① 组合类断言用
> `_expected_ids(seed_payload, ...)` 从种子数据独立算期望，而不是写死 id 列表；
> ② 脚本强制测试文件零改动；③ 修改后人工核对实现里没有特判测试数据（`grep p-10 / test_` 为空）。

## 3. 两轮的实际过程

| 轮次 | exit | 测试结果 | prompt tokens | completion tokens | 耗时 | 结果 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 1 | 30 passed / 1 failed | 13,084 | 4,014 | 93.7 s | `test_more_than_two_decimal_places_is_rejected` 失败 |
| 2 | 0 | 31 passed / 0 failed | 15,287（其中 12,597 命中缓存） | 4,534 | 107.7 s | 通过 |
| **合计** | | | **28,371** | **8,548** | **201 s** | `total_tokens=36,919` |

### 第 1 轮失败原因（可复现）

```bash
python scripts/replay_minimax.py --round 1      # 重放第 1 轮输出并跑测试
# 1 failed, 30 passed in 0.67s
# FAILED workdir/tests/test_search_target.py::test_more_than_two_decimal_places_is_rejected
# 复现成功：第 1 轮（预期失败，实际 exit=1 passed=30 failed=1）
```

日志：`docs/logs/20261028-140440-round1-repro.log`。

根因：第 1 轮用 `Decimal(...).quantize(Decimal("0.01"))` 解析价格，`price_min=1.999` 被**静默四舍五入**
成 `2.00` 而不是报 400，违反 FILTER-001「最多两位小数」与 FILTER-003「非法即 400」。

### 第 2 轮的修正（模型自述，见 `final-patch-response.json.notes`）

> 关键修正：在 parse_price 中用字符串检查代替 Decimal 自动量化。对于超过两位小数（如 1.999）
> 或含逗号（如 1,299）的非法格式，直接返回 None 让调用方抛出 INVALID_FILTER 错误。

修正后实现要点（与规格逐条对应）：

| 规格 | 实现落点 |
| --- | --- |
| FILTER-001 价格区间 | `filters.py::parse_price` + `parse_price_range` + `apply_price_range`（元→Decimal，闭区间比较，最多两位小数） |
| FILTER-002 仅看有货 | `filters.py::parse_in_stock_only`（`true/1/yes` ↔ `false/0/no` 双向集合）+ `apply_in_stock` |
| FILTER-003 非法参数 | `api.py::handle_products_query` 捕获 `ApiError` → 400；`ApiError("INVALID_FILTER", ...)` |
| FILTER-004 组合与 total | `service.py::search_products` 顺序：keyword → 价格 → 库存 → 排序 → 分页；`total` 取过滤后长度 |
| ADR-0011 旧参数忽略 | `api.py` 只读 `price_min/price_max`，注释显式说明 `min_price/max_price` 被忽略 |
| COMPAT-001..004 | 既有 `keyword / sort / page / page_size` 分支未改语义；响应结构 `to_public_dict` 未动 |

## 4. 结果与 diff 的对应关系

- 改动文件 3 个，全部在允许范围内：`workdir/biz/catalog/{service,api,filters}.py`；
- 测试文件 0 改动（`git status` / 摘要比对可证）；
- 统一 diff：`data/patches/0001-price-and-stock-filter.patch`（8,750 字节，`a/` `b/` 前缀，LF 行尾）。

**diff 与代码一致性验证（评审可自行复跑）**：

```bash
python scripts/replay_minimax.py --check
# [3/5] diff 校验：通过 · 0001-price-and-stock-filter.patch 可干净应用到基线，且结果与 MiniMax 输出一致
# [4/5] 摘要校验：通过（比对 docs/minimax/applied-digests.json 的 sha256）
# [5/5] pytest：exit=0 passed=31 failed=0
```

即：把 diff 打到 `data/baseline/` 上得到的三份文件，与提交入库的代码逐字节相同；
而这三份文件又等于当时 MiniMax 输出的 `content`。三者闭环，说明「记录里的模型输出」就是「提交的代码」。

## 5. 模型没有做的事（边界声明）

- 没有生成测试：测试是赛题任务的既有资产，修改前后一致；
- 没有改 `conftest.py`、没有新增文件、没有引入依赖：`scripts/minimax_change.py::_validate_files`
  会对任何越界路径直接报错并扔掉该轮输出；
- 没有修改规格：规格更新是另一条演示链路（`scripts/spec_update_demo.py`），与本次改码无关；
- 没有接触数据库：模型只看到上下文与源码，检索/落库都由模块完成。

## 6. 复跑方式（可选）

```bash
export MINIMAX_API_KEY=<your-token>
python scripts/minimax_change.py --task-id T-2026-1028 --apply
```

重跑会产生新的时间戳记录，并覆盖 `workdir/biz/` 与 patch；模型有随机性，重跑结果可能不同
（评测不需要重跑，用第 4 节的 replay 即可验证一致性）。
