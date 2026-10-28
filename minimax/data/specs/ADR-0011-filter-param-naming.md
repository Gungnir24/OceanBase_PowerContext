---
doc_id: adr-0011
title: ADR-0011 检索参数命名统一为 price_min / price_max
version: 1.1.0
status: 生效
owner: catalog-team
updated: 2026-03-04
supersedes: ADR-0007
---

# ADR-0011 检索参数命名统一为 price_min / price_max

### ADR-0011 检索参数命名统一为 price_min / price_max
- 状态: 生效
- 适用: GET /api/products
- 关联代码: workdir/biz/catalog/service.py#search_products, workdir/biz/catalog/api.py#handle_products_query
- 关联测试: workdir/tests/test_search_target.py#test_deprecated_parameter_names_are_ignored
- 正文:
  - 2026-03-04 决策：价格区间参数统一命名为 price_min 与 price_max。
  - 理由：与 OpenAPI 生成器、以及其余服务的下界/上界命名习惯一致。
  - 旧参数 min_price / max_price 不再受支持；收到时**必须忽略**，不得静默生效。
  - 本决策取代 ADR-0007；ADR-0007 的实现代码不得出现在仓库中。
