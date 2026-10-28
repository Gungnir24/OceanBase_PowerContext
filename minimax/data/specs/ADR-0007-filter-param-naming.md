---
doc_id: adr-0007
title: ADR-0007 检索参数命名使用 min_price / max_price
version: 1.0.0
status: 已取代
owner: catalog-team
updated: 2025-11-18
superseded_by: ADR-0011
---

# ADR-0007 检索参数命名使用 min_price / max_price

> **状态：已被 ADR-0011 取代。本条目仅作历史留档，不得据此实现。**

### ADR-0007 检索参数命名使用 min_price / max_price
- 状态: 已取代
- 适用: GET /api/products
- 取代: ADR-0011
- 正文:
  - 2025-11-18 决策：价格过滤参数命名为 min_price 与 max_price。
  - 当时的考虑是与内部报表接口的命名保持一致。
  - 该命名与 OpenAPI 生成器的风格不一致，已在 ADR-0011 中推翻。
  - 保留本条目的目的是让后来者知道「为什么曾经这么叫」，而不是继续使用它。
