---
doc_id: test-plan
title: 商品目录检索测试计划
version: 1.4.0
status: 生效
owner: qa-team
updated: 2026-09-30
---

# 商品目录检索测试计划

本计划规定「哪些测试证明哪条规格」。目标测试与回归测试**修改前后使用同一份文件**，
不得通过删除断言、跳过用例或修改预期值来制造通过。

### TEST-001 目标功能测试
- 状态: 生效
- 适用: 全局
- 关联测试: workdir/tests/test_search_target.py
- 正文:
  - 覆盖 FILTER-001、FILTER-002、FILTER-003、FILTER-004 与 ADR-0011。
  - 修改前应全部失败（目标行为尚未实现），修改后必须全部通过。
  - 禁止用 xfail / skip / 修改断言让用例「通过」。

### TEST-002 兼容性回归测试
- 状态: 生效
- 适用: 全局
- 关联测试: workdir/tests/test_search_regression.py
- 正文:
  - 覆盖 COMPAT-001、COMPAT-002、COMPAT-003、COMPAT-004。
  - 修改前后都必须全部通过；回归失败说明改坏了既有行为。

### TEST-003 服务可运行性冒烟
- 状态: 生效
- 适用: 全局
- 关联测试: workdir/tests/test_search_regression.py#test_http_server_serves_health_and_products
- 正文:
  - 必须能真实启动 HTTP 服务并响应 /health 与 /api/products。
  - 冒烟测试失败时，不允许把「缺依赖 / 起不来」解释成「功能未实现」。
