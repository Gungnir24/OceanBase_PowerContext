---
doc_id: srs-catalog
title: 商品目录检索需求规格说明书
version: 1.6.0
status: 生效
owner: product-team
updated: 2026-09-12
---

# 商品目录检索需求规格说明书

本文件记录**业务侧**要什么，接口细节见 `API-catalog-v2.md`。业务条目编号为
`SRS-F-*`，技术实现不得脱离这些条目自行发挥。

### SRS-F-014 支持按价格与库存筛选商品
- 状态: 生效
- 适用: GET /api/products
- 关联代码: workdir/biz/catalog/service.py#search_products, workdir/biz/catalog/filters.py
- 关联测试: workdir/tests/test_search_target.py
- 正文:
  - 运营在商品列表页希望按「价格区间」和「是否有货」筛选，避免逐页翻找。
  - 价格区间由运营在页面上以元输入，允许只填下界或只填上界。
  - 有货筛选是一个开关，默认关闭。
  - 验收以 workdir/tests/test_search_target.py 为准；不得改动既有的响应结构与排序语义。

### SRS-F-015 检索结果需保持可解释
- 状态: 生效
- 适用: 全局
- 关联代码: workdir/biz/catalog/service.py#search_products
- 正文:
  - 每次筛选的结果必须能解释：为什么这个商品被保留、为什么被排除。
  - 因此过滤条件之间必须正交，不得出现「加了价格条件导致 keyword 失效」这类耦合。
  - total 必须等于「通过全部过滤条件的商品数」。

### SRS-F-016 错误必须可判定
- 状态: 生效
- 适用: 全局
- 正文:
  - 前端需要依据错误码区分「参数写错」与「服务异常」。
  - 参数写错统一 400 + INVALID_FILTER / INVALID_SORT / INVALID_PAGINATION。
