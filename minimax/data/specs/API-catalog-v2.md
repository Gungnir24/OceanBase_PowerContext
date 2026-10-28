---
doc_id: api-catalog
title: 商品目录检索接口契约
version: 2.3.0
status: 生效
owner: catalog-team
updated: 2026-09-30
---

# 商品目录检索接口契约

本文件是 `catalog` 服务对外行为**唯一的接口事实来源**。任何改变
`GET /api/products` 入参、出参、排序或错误码的改动，都必须先更新本文件，
再改代码与测试；代码、测试、规格三者出现不一致时，以本文件的「生效」条目为准。

条目编号规则：`COMPAT-*` 为既有兼容性约束，`FILTER-*` 为本次新增的过滤能力。

### COMPAT-001 关键字匹配
- 状态: 生效
- 适用: 全局
- 关联代码: workdir/biz/catalog/filters.py#matches_keyword, workdir/biz/catalog/filters.py#apply_keyword
- 关联测试: workdir/tests/test_search_regression.py#test_keyword_matches_name, workdir/tests/test_search_regression.py#test_keyword_is_case_insensitive_across_tags, workdir/tests/test_search_regression.py#test_keyword_without_match_returns_empty_page, workdir/tests/test_search_regression.py#test_relevance_order_keeps_seed_order_after_keyword
- 正文:
  - keyword 为大小写不敏感的子串匹配，命中商品名或任意 tag 即算命中。
  - 空 keyword 等价于不过滤，且不得改变返回顺序。
  - 匹配后必须保持种子顺序（relevance 语义）。

### COMPAT-002 排序
- 状态: 生效
- 适用: 全局
- 关联代码: workdir/biz/catalog/filters.py#sort_products, workdir/biz/catalog/filters.py#SORT_OPTIONS
- 关联测试: workdir/tests/test_search_regression.py#test_sort_price_asc, workdir/tests/test_search_regression.py#test_sort_price_desc, workdir/tests/test_search_regression.py#test_sort_name_asc, workdir/tests/test_search_regression.py#test_unknown_sort_is_rejected
- 正文:
  - sort 仅支持 relevance / price_asc / price_desc / name_asc，缺省为 relevance。
  - 传入其它取值返回 400，错误码 INVALID_SORT。
  - price_asc 与 price_desc 按 price_cents 比较，价格相同按 id 升序，保证排序稳定。
  - relevance 不得重新排序，保持仓储返回顺序。

### COMPAT-003 响应结构
- 状态: 生效
- 适用: GET /api/products
- 关联代码: workdir/biz/catalog/service.py#search_products, workdir/biz/catalog/models.py#Product.to_public_dict
- 关联测试: workdir/tests/test_search_regression.py#test_response_shape_is_stable, workdir/tests/test_search_regression.py#test_default_query_returns_first_page
- 正文:
  - 成功响应固定为 `{"items", "total", "page", "page_size"}` 四个字段，不得新增或改名。
  - items 的元素固定包含 id/name/price/stock/category/tags；price 为以元为单位的浮点数。
  - 错误响应固定为 `{"error": {"code": ..., "message": ...}}`。

### COMPAT-004 分页
- 状态: 生效
- 适用: GET /api/products
- 关联代码: workdir/biz/catalog/service.py#parse_page, workdir/biz/catalog/service.py#parse_page_size, workdir/biz/catalog/service.py#paginate
- 关联测试: workdir/tests/test_search_regression.py#test_pagination_slices_without_changing_total, workdir/tests/test_search_regression.py#test_page_size_above_cap_is_rejected, workdir/tests/test_search_regression.py#test_page_zero_is_rejected
- 正文:
  - page 默认 1，page_size 默认 20，page_size 上限 100。
  - 非法分页参数返回 400，错误码 INVALID_PAGINATION。
  - total 是过滤完成、分页之前的总数。
  - page 超出范围时返回空 items，不报错。

### FILTER-001 价格区间筛选
- 状态: 生效
- 适用: GET /api/products
- 关联代码: workdir/biz/catalog/filters.py, workdir/biz/catalog/service.py#search_products
- 关联测试: workdir/tests/test_search_target.py#test_price_range_is_inclusive_on_both_bounds, workdir/tests/test_search_target.py#test_price_min_only, workdir/tests/test_search_target.py#test_price_max_only, workdir/tests/test_search_target.py#test_decimal_bounds_keep_two_digit_precision, workdir/tests/test_search_target.py#test_zero_lower_bound_keeps_all_products
- 正文:
  - 新增查询参数 price_min 与 price_max，单位元，以字符串传入。
  - 区间为闭区间：price_min <= 商品价格 <= price_max，边界值必须保留。
  - 最多两位小数；超过两位（如 1.999）视为非法。
  - 0 是合法下界；两个参数都缺省时不做价格过滤。
  - 商品价格在库内以「分」为整数存储，比较前把元换算成分（四舍五入到分）。

### FILTER-002 仅看有货
- 状态: 生效
- 适用: GET /api/products
- 关联代码: workdir/biz/catalog/filters.py, workdir/biz/catalog/service.py#search_products
- 关联测试: workdir/tests/test_search_target.py#test_in_stock_only_true_filters_out_sold_out, workdir/tests/test_search_target.py#test_in_stock_only_accepts_yes_and_one, workdir/tests/test_search_target.py#test_in_stock_only_false_is_a_noop
- 正文:
  - 新增查询参数 in_stock_only。
  - 真值集合：true / 1 / yes（大小写不敏感）；假值集合：false / 0 / no。
  - 取真值时只返回 stock > 0 的商品；取假值或省略时不过滤。
  - 其它取值属于非法参数，按 FILTER-003 处理。

### FILTER-003 非法过滤参数
- 状态: 生效
- 适用: GET /api/products
- 关联代码: workdir/biz/catalog/errors.py#ApiError, workdir/biz/catalog/api.py#handle_products_query
- 关联测试: workdir/tests/test_search_target.py#test_invalid_price_range_returns_invalid_filter, workdir/tests/test_search_target.py#test_negative_price_returns_invalid_filter, workdir/tests/test_search_target.py#test_non_numeric_price_returns_invalid_filter, workdir/tests/test_search_target.py#test_more_than_two_decimal_places_is_rejected, workdir/tests/test_search_target.py#test_invalid_in_stock_value_returns_invalid_filter
- 正文:
  - 价格参数不是数字、为负、或小数位超过两位时，返回 400，错误码 INVALID_FILTER。
  - price_min 大于 price_max 时，返回 400，错误码 INVALID_FILTER。
  - in_stock_only 取值不在真值/假值集合内时，返回 400，错误码 INVALID_FILTER。
  - 不得用 500、静默忽略或空结果集代替错误响应。

### FILTER-004 组合与 total 语义
- 状态: 生效
- 适用: GET /api/products
- 关联代码: workdir/biz/catalog/service.py#search_products
- 关联测试: workdir/tests/test_search_target.py#test_filter_combines_with_sort_and_pagination, workdir/tests/test_search_target.py#test_filter_combines_with_keyword, workdir/tests/test_search_target.py#test_total_is_counted_after_filter_and_page_can_overflow
- 正文:
  - 过滤顺序固定为：keyword -> 价格区间 -> 库存 -> 排序 -> 分页。
  - total 必须是全部过滤完成后的总数，与 page/page_size 无关。
  - 新增过滤条件必须能与 keyword、sort、page、page_size 任意组合，不得互相覆盖。
