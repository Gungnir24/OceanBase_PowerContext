# SDD 开发上下文 · T-2026-1028

- 任务：为 GET /api/products 增加按价格区间（price_min / price_max，单位元、闭区间、最多两位小数）与库存状态（in_stock_only）的筛选条件；非法过滤参数返回 400 且错误码为 INVALID_FILTER；过滤需与 keyword / sort / page / page_size 组合，total 为过滤后总数；同时保持既有响应结构、排序语义与分页行为不变。
- 接口：/api/products
- 生成时间：2026-10-28T14:14:03

## 1. 适用规格（15 条）

### ADR-0011 检索参数命名统一为 price_min / price_max
- 来源：`data/specs/ADR-0011-filter-param-naming.md` · 版本 1.1.0 · 状态 active · 适用 GET /api/products
- 选择依据：适用面 + 全文/向量
- 检索相关度：0.03055
- 要求：
  - 2026-03-04 决策：价格区间参数统一命名为 price_min 与 price_max。
  - 理由：与 OpenAPI 生成器、以及其余服务的下界/上界命名习惯一致。
  - 旧参数 min_price / max_price 不再受支持；收到时**必须忽略**，不得静默生效。
  - 本决策取代 ADR-0007；ADR-0007 的实现代码不得出现在仓库中。
- 声明关联代码：workdir/biz/catalog/service.py#search_products, workdir/biz/catalog/api.py#handle_products_query
- 声明关联测试：workdir/tests/test_search_target.py#test_deprecated_parameter_names_are_ignored

### COMPAT-001 关键字匹配
- 来源：`data/specs/API-catalog-v2.md` · 版本 2.3.0 · 状态 active · 适用 全局
- 选择依据：适用面
- 要求：
  - keyword 为大小写不敏感的子串匹配，命中商品名或任意 tag 即算命中。
  - 空 keyword 等价于不过滤，且不得改变返回顺序。
  - 匹配后必须保持种子顺序（relevance 语义）。
- 声明关联代码：workdir/biz/catalog/filters.py#matches_keyword, workdir/biz/catalog/filters.py#apply_keyword
- 声明关联测试：workdir/tests/test_search_regression.py#test_keyword_matches_name, workdir/tests/test_search_regression.py#test_keyword_is_case_insensitive_across_tags, workdir/tests/test_search_regression.py#test_keyword_without_match_returns_empty_page, workdir/tests/test_search_regression.py#test_relevance_order_keeps_seed_order_after_keyword

### COMPAT-002 排序
- 来源：`data/specs/API-catalog-v2.md` · 版本 2.3.0 · 状态 active · 适用 全局
- 选择依据：适用面 + 全文/向量
- 检索相关度：0.028577
- 要求：
  - sort 仅支持 relevance / price_asc / price_desc / name_asc，缺省为 relevance。
  - 传入其它取值返回 400，错误码 INVALID_SORT。
  - price_asc 与 price_desc 按 price_cents 比较，价格相同按 id 升序，保证排序稳定。
  - relevance 不得重新排序，保持仓储返回顺序。
- 声明关联代码：workdir/biz/catalog/filters.py#sort_products, workdir/biz/catalog/filters.py#SORT_OPTIONS
- 声明关联测试：workdir/tests/test_search_regression.py#test_sort_price_asc, workdir/tests/test_search_regression.py#test_sort_price_desc, workdir/tests/test_search_regression.py#test_sort_name_asc, workdir/tests/test_search_regression.py#test_unknown_sort_is_rejected

### COMPAT-003 响应结构
- 来源：`data/specs/API-catalog-v2.md` · 版本 2.3.0 · 状态 active · 适用 GET /api/products
- 选择依据：适用面 + 全文/向量
- 检索相关度：0.029857
- 要求：
  - 成功响应固定为 `{"items", "total", "page", "page_size"}` 四个字段，不得新增或改名。
  - items 的元素固定包含 id/name/price/stock/category/tags；price 为以元为单位的浮点数。
  - 错误响应固定为 `{"error": {"code": ..., "message": ...}}`。
- 声明关联代码：workdir/biz/catalog/service.py#search_products, workdir/biz/catalog/models.py#Product.to_public_dict
- 声明关联测试：workdir/tests/test_search_regression.py#test_response_shape_is_stable, workdir/tests/test_search_regression.py#test_default_query_returns_first_page

### COMPAT-004 分页
- 来源：`data/specs/API-catalog-v2.md` · 版本 2.3.0 · 状态 active · 适用 GET /api/products
- 选择依据：适用面 + 全文/向量
- 检索相关度：0.015625
- 要求：
  - page 默认 1，page_size 默认 20，page_size 上限 100。
  - 非法分页参数返回 400，错误码 INVALID_PAGINATION。
  - total 是过滤完成、分页之前的总数。
  - page 超出范围时返回空 items，不报错。
- 声明关联代码：workdir/biz/catalog/service.py#parse_page, workdir/biz/catalog/service.py#parse_page_size, workdir/biz/catalog/service.py#paginate
- 声明关联测试：workdir/tests/test_search_regression.py#test_pagination_slices_without_changing_total, workdir/tests/test_search_regression.py#test_page_size_above_cap_is_rejected, workdir/tests/test_search_regression.py#test_page_zero_is_rejected

### FILTER-001 价格区间筛选
- 来源：`data/specs/API-catalog-v2.md` · 版本 2.3.0 · 状态 active · 适用 GET /api/products
- 选择依据：适用面 + 全文/向量
- 检索相关度：0.032002
- 要求：
  - 新增查询参数 price_min 与 price_max，单位元，以字符串传入。
  - 区间为闭区间：price_min <= 商品价格 <= price_max，边界值必须保留。
  - 最多两位小数；超过两位（如 1.999）视为非法。
  - 0 是合法下界；两个参数都缺省时不做价格过滤。
  - 商品价格在库内以「分」为整数存储，比较前把元换算成分（四舍五入到分）。
- 声明关联代码：workdir/biz/catalog/filters.py, workdir/biz/catalog/service.py#search_products
- 声明关联测试：workdir/tests/test_search_target.py#test_price_range_is_inclusive_on_both_bounds, workdir/tests/test_search_target.py#test_price_min_only, workdir/tests/test_search_target.py#test_price_max_only, workdir/tests/test_search_target.py#test_decimal_bounds_keep_two_digit_precision, workdir/tests/test_search_target.py#test_zero_lower_bound_keeps_all_products

### FILTER-002 仅看有货
- 来源：`data/specs/API-catalog-v2.md` · 版本 2.3.0 · 状态 active · 适用 GET /api/products
- 选择依据：适用面 + 全文/向量
- 检索相关度：0.014493
- 要求：
  - 新增查询参数 in_stock_only。
  - 真值集合：true / 1 / yes（大小写不敏感）；假值集合：false / 0 / no。
  - 取真值时只返回 stock > 0 的商品；取假值或省略时不过滤。
  - 其它取值属于非法参数，按 FILTER-003 处理。
- 声明关联代码：workdir/biz/catalog/filters.py, workdir/biz/catalog/service.py#search_products
- 声明关联测试：workdir/tests/test_search_target.py#test_in_stock_only_true_filters_out_sold_out, workdir/tests/test_search_target.py#test_in_stock_only_accepts_yes_and_one, workdir/tests/test_search_target.py#test_in_stock_only_false_is_a_noop

### FILTER-003 非法过滤参数
- 来源：`data/specs/API-catalog-v2.md` · 版本 2.3.0 · 状态 active · 适用 GET /api/products
- 选择依据：适用面 + 全文/向量
- 检索相关度：0.031545
- 要求：
  - 价格参数不是数字、为负、或小数位超过两位时，返回 400，错误码 INVALID_FILTER。
  - price_min 大于 price_max 时，返回 400，错误码 INVALID_FILTER。
  - in_stock_only 取值不在真值/假值集合内时，返回 400，错误码 INVALID_FILTER。
  - 不得用 500、静默忽略或空结果集代替错误响应。
- 声明关联代码：workdir/biz/catalog/errors.py#ApiError, workdir/biz/catalog/api.py#handle_products_query
- 声明关联测试：workdir/tests/test_search_target.py#test_invalid_price_range_returns_invalid_filter, workdir/tests/test_search_target.py#test_negative_price_returns_invalid_filter, workdir/tests/test_search_target.py#test_non_numeric_price_returns_invalid_filter, workdir/tests/test_search_target.py#test_more_than_two_decimal_places_is_rejected, workdir/tests/test_search_target.py#test_invalid_in_stock_value_returns_invalid_filter

### FILTER-004 组合与 total 语义
- 来源：`data/specs/API-catalog-v2.md` · 版本 2.3.0 · 状态 active · 适用 GET /api/products
- 选择依据：适用面 + 全文/向量
- 检索相关度：0.032002
- 要求：
  - 过滤顺序固定为：keyword -> 价格区间 -> 库存 -> 排序 -> 分页。
  - total 必须是全部过滤完成后的总数，与 page/page_size 无关。
  - 新增过滤条件必须能与 keyword、sort、page、page_size 任意组合，不得互相覆盖。
- 声明关联代码：workdir/biz/catalog/service.py#search_products
- 声明关联测试：workdir/tests/test_search_target.py#test_filter_combines_with_sort_and_pagination, workdir/tests/test_search_target.py#test_filter_combines_with_keyword, workdir/tests/test_search_target.py#test_total_is_counted_after_filter_and_page_can_overflow

### SRS-F-014 支持按价格与库存筛选商品
- 来源：`data/specs/SRS-catalog.md` · 版本 1.6.0 · 状态 active · 适用 GET /api/products
- 选择依据：适用面 + 全文/向量
- 检索相关度：0.031778
- 要求：
  - 运营在商品列表页希望按「价格区间」和「是否有货」筛选，避免逐页翻找。
  - 价格区间由运营在页面上以元输入，允许只填下界或只填上界。
  - 有货筛选是一个开关，默认关闭。
  - 验收以 workdir/tests/test_search_target.py 为准；不得改动既有的响应结构与排序语义。
- 声明关联代码：workdir/biz/catalog/service.py#search_products, workdir/biz/catalog/filters.py
- 声明关联测试：workdir/tests/test_search_target.py

### SRS-F-015 检索结果需保持可解释
- 来源：`data/specs/SRS-catalog.md` · 版本 1.6.0 · 状态 active · 适用 全局
- 选择依据：适用面 + 全文/向量
- 检索相关度：0.03009
- 要求：
  - 每次筛选的结果必须能解释：为什么这个商品被保留、为什么被排除。
  - 因此过滤条件之间必须正交，不得出现「加了价格条件导致 keyword 失效」这类耦合。
  - total 必须等于「通过全部过滤条件的商品数」。
- 声明关联代码：workdir/biz/catalog/service.py#search_products

### SRS-F-016 错误必须可判定
- 来源：`data/specs/SRS-catalog.md` · 版本 1.6.0 · 状态 active · 适用 全局
- 选择依据：适用面 + 全文/向量
- 检索相关度：0.014286
- 要求：
  - 前端需要依据错误码区分「参数写错」与「服务异常」。
  - 参数写错统一 400 + INVALID_FILTER / INVALID_SORT / INVALID_PAGINATION。

### TEST-001 目标功能测试
- 来源：`data/specs/TEST-PLAN-catalog.md` · 版本 1.4.0 · 状态 active · 适用 全局
- 选择依据：适用面
- 要求：
  - 覆盖 FILTER-001、FILTER-002、FILTER-003、FILTER-004 与 ADR-0011。
  - 修改前应全部失败（目标行为尚未实现），修改后必须全部通过。
  - 禁止用 xfail / skip / 修改断言让用例「通过」。
- 声明关联测试：workdir/tests/test_search_target.py

### TEST-002 兼容性回归测试
- 来源：`data/specs/TEST-PLAN-catalog.md` · 版本 1.4.0 · 状态 active · 适用 全局
- 选择依据：适用面
- 要求：
  - 覆盖 COMPAT-001、COMPAT-002、COMPAT-003、COMPAT-004。
  - 修改前后都必须全部通过；回归失败说明改坏了既有行为。
- 声明关联测试：workdir/tests/test_search_regression.py

### TEST-003 服务可运行性冒烟
- 来源：`data/specs/TEST-PLAN-catalog.md` · 版本 1.4.0 · 状态 active · 适用 全局
- 选择依据：适用面
- 要求：
  - 必须能真实启动 HTTP 服务并响应 /health 与 /api/products。
  - 冒烟测试失败时，不允许把「缺依赖 / 起不来」解释成「功能未实现」。
- 声明关联测试：workdir/tests/test_search_regression.py#test_http_server_serves_health_and_products

## 2. 需要修改的代码（18 处）

### workdir/biz/catalog/service.py#search_products
- 行号：L147-L186
- 入选原因：规格 ADR-0011 声明的关联代码
- 依据条目：ADR-0011, COMPAT-003, FILTER-001, FILTER-002, FILTER-004, SRS-F-014, SRS-F-015

```python
def search_products(
    repo: ProductRepository,
    *,
    keyword: str | None = "",
    sort: str | None = "relevance",
    page: int = DEFAULT_PAGE,
    page_size: int = DEFAULT_PAGE_SIZE,
    price_min: str | None = None,
    price_max: str | None = None,
    in_stock_only: str | None = None,
) -> dict:
    """返回 ``{"items": [...], "total": int, "page": int, "page_size": int}``。

    过滤顺序（FILTER-004）：keyword -> 价格区间 -> 库存 -> 排序 -> 分页。
    """
    # 解析并验证价格区间参数
    min_cents, max_cents = parse_price_range(price_min, price_max)

    # 解析库存状态参数
    stock_filter = parse_in_stock_only(in_stock_only)

    products = repo.all()
    # 1) keyword 过滤（COMPAT-001）
    products = apply_keyword(products, keyword)
    # 2) 价格区间过滤（FILTER-001）
    products = apply_price_range(products, min_cents, max_cents)
    # 3) 库存状态过滤（FILTER-002）
    products = apply_in_stock(products, stock_filter)
    # 4) 排序（COMPAT-002）
    products = sort_products(products, sort)

    # total 是全部过滤完成后的总数，与分页无关（FILTER-004）
    total = len(products)
    page_items = paginate(products, page, page_size)
    return {
        "items": [product.to_public_dict() for product in page_items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }
```

### workdir/biz/catalog/api.py#handle_products_query
- 行号：L26-L57
- 入选原因：规格 ADR-0011 声明的关联代码
- 依据条目：ADR-0011, FILTER-003

```python
def handle_products_query(repo: ProductRepository, query: Mapping[str, str]) -> tuple[int, dict]:
    """处理 ``GET /api/products``。

    ``query`` 是已经解码的 query string（多值取第一个）。

    新增筛选参数（FILTER-001, FILTER-002）：
    * price_min / price_max：价格区间（单位元，闭区间）。
    * in_stock_only：仅返回有货商品（true/1/yes 为真，false/0/no 为假，其余非法）。
    * 旧参数 min_price / max_price 按 ADR-0011 必须忽略。
    """
    try:
        page = parse_page(query.get("page", DEFAULT_PAGE))
        page_size = parse_page_size(query.get("page_size", DEFAULT_PAGE_SIZE))

        # ADR-0011：忽略旧参数名 min_price / max_price
        price_min = query.get("price_min")
        price_max = query.get("price_max")
        in_stock_only = query.get("in_stock_only")

        payload = search_products(
            repo,
            keyword=query.get("keyword", ""),
            sort=query.get("sort", "relevance"),
            page=page,
            page_size=page_size,
            price_min=price_min,
            price_max=price_max,
            in_stock_only=in_stock_only,
        )
        return 200, payload
    except ApiError as exc:
        return exc.status, exc.to_payload()
```

### workdir/biz/catalog/filters.py#matches_keyword
- 行号：L29-L36
- 入选原因：规格 COMPAT-001 声明的关联代码
- 依据条目：COMPAT-001, FILTER-001, FILTER-002, SRS-F-014

```python
def matches_keyword(product: Product, keyword: str | None) -> bool:
    """大小写不敏感的子串匹配：命中商品名或任意 tag 即算命中。"""
    needle = normalize_keyword(keyword)
    if not needle:
        return True
    if needle in product.name.lower():
        return True
    return any(needle in tag.lower() for tag in product.tags)
```

### workdir/biz/catalog/filters.py#apply_keyword
- 行号：L39-L40
- 入选原因：规格 COMPAT-001 声明的关联代码
- 依据条目：COMPAT-001, FILTER-001, FILTER-002, SRS-F-014

```python
def apply_keyword(products: Iterable[Product], keyword: str | None) -> list[Product]:
    return [product for product in products if matches_keyword(product, keyword)]
```

### workdir/biz/catalog/filters.py#sort_products
- 行号：L71-L86
- 入选原因：规格 COMPAT-002 声明的关联代码
- 依据条目：COMPAT-002, FILTER-001, FILTER-002, SRS-F-014

```python
def sort_products(products: list[Product], sort: str | None) -> list[Product]:
    """按 ``sort`` 排序；未知取值属于非法请求。"""
    key = (sort or DEFAULT_SORT).strip().lower()
    if key not in SORT_OPTIONS:
        raise ApiError(
            "INVALID_SORT",
            f"sort 仅支持 {', '.join(SORT_OPTIONS)}，收到 {sort!r}",
        )
    if key == "relevance":
        # 默认顺序即种子顺序，保持稳定，不能因为过滤而被打乱。
        return list(products)
    if key == "price_asc":
        return sorted(products, key=lambda product: (product.price_cents, product.id))
    if key == "price_desc":
        return sorted(products, key=lambda product: (-product.price_cents, product.id))
    return sorted(products, key=lambda product: (product.name, product.id))
```

### workdir/biz/catalog/filters.py#SORT_OPTIONS
- 行号：L21-L21
- 入选原因：规格 COMPAT-002 声明的关联代码
- 依据条目：COMPAT-002

```python
SORT_OPTIONS: tuple[str, ...] = ("relevance", "price_asc", "price_desc", "name_asc")
```

### workdir/biz/catalog/models.py#Product.to_public_dict
- 行号：L27-L36
- 入选原因：规格 COMPAT-003 声明的关联代码
- 依据条目：COMPAT-003

```python
def to_public_dict(self) -> dict[str, Any]:
        """对外响应里的商品字段；字段集合属于兼容性契约。"""
        return {
            "id": self.id,
            "name": self.name,
            "price": self.price_yuan,
            "stock": self.stock,
            "category": self.category,
            "tags": list(self.tags),
        }
```

### workdir/biz/catalog/service.py#parse_page
- 行号：L31-L40
- 入选原因：规格 COMPAT-004 声明的关联代码
- 依据条目：COMPAT-004

```python
def parse_page(raw: str | int | None) -> int:
    if raw is None or raw == "":
        return DEFAULT_PAGE
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        raise ApiError("INVALID_PAGINATION", f"page 必须是正整数，收到 {raw!r}") from None
    if value < 1:
        raise ApiError("INVALID_PAGINATION", f"page 必须 >= 1，收到 {value}")
    return value
```

### workdir/biz/catalog/service.py#parse_page_size
- 行号：L43-L54
- 入选原因：规格 COMPAT-004 声明的关联代码
- 依据条目：COMPAT-004

```python
def parse_page_size(raw: str | int | None) -> int:
    if raw is None or raw == "":
        return DEFAULT_PAGE_SIZE
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        raise ApiError("INVALID_PAGINATION", f"page_size 必须是正整数，收到 {raw!r}") from None
    if value < 1:
        raise ApiError("INVALID_PAGINATION", f"page_size 必须 >= 1，收到 {value}")
    if value > MAX_PAGE_SIZE:
        raise ApiError("INVALID_PAGINATION", f"page_size 最大 {MAX_PAGE_SIZE}，收到 {value}")
    return value
```

### workdir/biz/catalog/service.py#paginate
- 行号：L140-L144
- 入选原因：规格 COMPAT-004 声明的关联代码
- 依据条目：COMPAT-004

```python
def paginate(products: list[Product], page: int, page_size: int) -> list[Product]:
    start = (page - 1) * page_size
    if start >= len(products):
        return []
    return products[start : start + page_size]
```

### workdir/biz/catalog/filters.py#normalize_keyword
- 行号：L25-L26
- 入选原因：规格 FILTER-001 声明的关联代码（文件级）
- 依据条目：FILTER-001, FILTER-002, SRS-F-014

```python
def normalize_keyword(keyword: str | None) -> str:
    return (keyword or "").strip().lower()
```

### workdir/biz/catalog/filters.py#apply_price_range
- 行号：L43-L57
- 入选原因：规格 FILTER-001 声明的关联代码（文件级）
- 依据条目：FILTER-001, FILTER-002, SRS-F-014

```python
def apply_price_range(
    products: Iterable[Product], min_cents: int | None, max_cents: int | None
) -> list[Product]:
    """按价格区间过滤（闭区间）。

    min_cents 为 None 表示无下界；max_cents 为 None 表示无上界。
    """
    result = []
    for product in products:
        if min_cents is not None and product.price_cents < min_cents:
            continue
        if max_cents is not None and product.price_cents > max_cents:
            continue
        result.append(product)
    return result
```

### workdir/biz/catalog/filters.py#apply_in_stock
- 行号：L60-L68
- 入选原因：规格 FILTER-001 声明的关联代码（文件级）
- 依据条目：FILTER-001, FILTER-002, SRS-F-014

```python
def apply_in_stock(products: Iterable[Product], in_stock_only: bool | None) -> list[Product]:
    """按库存状态过滤。

    in_stock_only 为 True 时只保留 stock > 0 的商品；
    为 False 或 None 时不过滤。
    """
    if in_stock_only is not True:
        return list(products)
    return [product for product in products if product.stock > 0]
```

### workdir/biz/catalog/errors.py#ApiError
- 行号：L6-L20
- 入选原因：规格 FILTER-003 声明的关联代码
- 依据条目：FILTER-003

```python
class ApiError(Exception):
    """带错误码的请求错误。

    ``code`` 会原样出现在响应体的 ``error.code`` 字段里，因此错误码本身
    属于对外契约的一部分，修改需要同步规格（见 data/specs/API-catalog-v2.md）。
    """

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
```

### workdir/biz/catalog/service.py#parse_price_range
- 入选原因：全文/向量相似（未经规格声明，供参考）

```python
def parse_price_range(
    price_min: str | None, price_max: str | None
) -> tuple[int | None, int | None]:
    """解析价格区间参数。

    返回 (min_cents, max_cents)；任一参数非法则抛出 ApiError(INVALID_FILTER)。
    """
    min_cents = parse_price(price_min)
    max_cents = parse_price(price_max)

    # 检查非数字/负数/超过两位小数的情况
    if price_min is not None and price_min.strip() != "" and min_cents is None:
        raise ApiError("INVALID_FILTER", f"price_min 必须是有效金额，收到 {price_min!r}")
    if price_max is not None and price_max.strip() != "" and max_cents is None:
        raise ApiError("INVALID_FILTER", f"price_max 必须是有效金额，收到 {price_max!r}")

    # 检查区间有效性
    if min_cents is not None and max_cents is not None and min_cents > max_cents:
        raise ApiError(
            "INVALID_FILTER",
            f"price_min({price_min}) 不能大于 price_max({price_max})",
        )

    return min_cents, max_cents
```

### workdir/biz/catalog/store.py#yuan_to_cents
- 入选原因：全文/向量相似（未经规格声明，供参考）

```python
def yuan_to_cents(raw: str | int | float | Decimal) -> int:
    """把「元」金额换算成「分」，按四舍五入保留两位小数。"""
    value = Decimal(str(raw)).quantize(Decimal("0.01"))
    return int(value * 100)
```

### workdir/biz/catalog/service.py#parse_price
- 入选原因：全文/向量相似（未经规格声明，供参考）

```python
def parse_price(raw: str | None) -> int | None:
    """把「元」字符串解析成「分」的整数。

    最多两位小数；负数、非数字、或超过两位小数视为非法，返回 None（由调用方抛出 ApiError）。
    """
    if raw is None or raw.strip() == "":
        return None
    s = raw.strip()
    try:
        # Decimal 会把逗号当千位分隔符（如 '1,299' -> 1299），按规格这应该被拒绝。
        # 先用字符串检查：只允许数字、单个可选小数点、前面可选负号。
        if "." in s:
            integer_part, fractional_part = s.split(".", 1)
            # 整数部分只允许数字，可选前缀负号
            if integer_part.startswith("-"):
                integer_digits = integer_part[1:]
            else:
                integer_digits = integer_part
            if not integer_digits.isdigit() or len(fractional_part) > 2:
                return None
        else:
            # 无小数点：必须是纯数字（可选负号）
            if s.startswith("-"):
                digits = s[1:]
            else:
                digits = s
            if not digits.isdigit():
                return None

        value = Decimal(s)
        if value < 0:
            return None
        # 量化到两位小数，四舍五入
        quantized = value.quantize(Decimal("0.01"))
        return int(quantized * 100)
    except (InvalidOperation, ValueError):
        return None
```

### workdir/biz/catalog/api.py#PRODUCTS_PATH
- 入选原因：全文/向量相似（未经规格声明，供参考）

```python
PRODUCTS_PATH = "/api/products"
```

## 3. 验证用测试（31 个）
- [target] `workdir/tests/test_search_target.py::test_deprecated_parameter_names_are_ignored` · 规格 ADR-0011 声明的验证用例
- [regression] `workdir/tests/test_search_regression.py::test_keyword_matches_name` · 规格 COMPAT-001 声明的验证用例
- [regression] `workdir/tests/test_search_regression.py::test_keyword_is_case_insensitive_across_tags` · 规格 COMPAT-001 声明的验证用例
- [regression] `workdir/tests/test_search_regression.py::test_keyword_without_match_returns_empty_page` · 规格 COMPAT-001 声明的验证用例
- [regression] `workdir/tests/test_search_regression.py::test_relevance_order_keeps_seed_order_after_keyword` · 规格 COMPAT-001 声明的验证用例
- [regression] `workdir/tests/test_search_regression.py::test_sort_price_asc` · 规格 COMPAT-002 声明的验证用例
- [regression] `workdir/tests/test_search_regression.py::test_sort_price_desc` · 规格 COMPAT-002 声明的验证用例
- [regression] `workdir/tests/test_search_regression.py::test_sort_name_asc` · 规格 COMPAT-002 声明的验证用例
- [regression] `workdir/tests/test_search_regression.py::test_unknown_sort_is_rejected` · 规格 COMPAT-002 声明的验证用例
- [regression] `workdir/tests/test_search_regression.py::test_response_shape_is_stable` · 规格 COMPAT-003 声明的验证用例
- [regression] `workdir/tests/test_search_regression.py::test_default_query_returns_first_page` · 规格 COMPAT-003 声明的验证用例
- [regression] `workdir/tests/test_search_regression.py::test_pagination_slices_without_changing_total` · 规格 COMPAT-004 声明的验证用例
- [regression] `workdir/tests/test_search_regression.py::test_page_size_above_cap_is_rejected` · 规格 COMPAT-004 声明的验证用例
- [regression] `workdir/tests/test_search_regression.py::test_page_zero_is_rejected` · 规格 COMPAT-004 声明的验证用例
- [target] `workdir/tests/test_search_target.py::test_price_range_is_inclusive_on_both_bounds` · 规格 FILTER-001 声明的验证用例
- [target] `workdir/tests/test_search_target.py::test_price_min_only` · 规格 FILTER-001 声明的验证用例
- [target] `workdir/tests/test_search_target.py::test_price_max_only` · 规格 FILTER-001 声明的验证用例
- [target] `workdir/tests/test_search_target.py::test_decimal_bounds_keep_two_digit_precision` · 规格 FILTER-001 声明的验证用例
- [target] `workdir/tests/test_search_target.py::test_zero_lower_bound_keeps_all_products` · 规格 FILTER-001 声明的验证用例
- [target] `workdir/tests/test_search_target.py::test_in_stock_only_true_filters_out_sold_out` · 规格 FILTER-002 声明的验证用例
- [target] `workdir/tests/test_search_target.py::test_in_stock_only_accepts_yes_and_one` · 规格 FILTER-002 声明的验证用例
- [target] `workdir/tests/test_search_target.py::test_in_stock_only_false_is_a_noop` · 规格 FILTER-002 声明的验证用例
- [target] `workdir/tests/test_search_target.py::test_invalid_price_range_returns_invalid_filter` · 规格 FILTER-003 声明的验证用例
- [target] `workdir/tests/test_search_target.py::test_negative_price_returns_invalid_filter` · 规格 FILTER-003 声明的验证用例
- [target] `workdir/tests/test_search_target.py::test_non_numeric_price_returns_invalid_filter` · 规格 FILTER-003 声明的验证用例
- [target] `workdir/tests/test_search_target.py::test_more_than_two_decimal_places_is_rejected` · 规格 FILTER-003 声明的验证用例
- [target] `workdir/tests/test_search_target.py::test_invalid_in_stock_value_returns_invalid_filter` · 规格 FILTER-003 声明的验证用例
- [target] `workdir/tests/test_search_target.py::test_filter_combines_with_sort_and_pagination` · 规格 FILTER-004 声明的验证用例
- [target] `workdir/tests/test_search_target.py::test_filter_combines_with_keyword` · 规格 FILTER-004 声明的验证用例
- [target] `workdir/tests/test_search_target.py::test_total_is_counted_after_filter_and_page_can_overflow` · 规格 FILTER-004 声明的验证用例
- [regression] `workdir/tests/test_search_regression.py::test_http_server_serves_health_and_products` · 规格 TEST-002 声明的验证用例

## 4. 任务状态（PowerContext）
- 任务：T-2026-1028 · 商品目录检索：新增价格区间与库存筛选
- Scope：`scp_1v16tw28vh35ygy2n5vdvf99ms` · 状态条目 13 条
- 已完成：
  - 实现 FILTER-001：price_min/price_max 闭区间过滤（元转分、最多两位小数）
  - MiniMax（minimax/minimax-m2.5）完成实现，2 轮内 31 个测试全部通过
  - 实现 FILTER-004：过滤顺序 keyword->价格->库存->排序->分页，total 取过滤后总数
  - 实现 FILTER-003：非法过滤参数返回 400 INVALID_FILTER
  - 实现 FILTER-002：in_stock_only 真值/假值集合与 stock>0 过滤
- 待办：
  - 成本优化：为 context 装配加一层按 task 的检索结果缓存，降低重复查询
  - 跨模块协作：订单服务复用同一套 INVALID_FILTER 错误码（待 ADR-0012 定稿）
- 已有验证结果：
  - baseline-regression：退出码 0，通过 14，失败 0 · 日志 docs/logs/20261028-141335-baseline-regression.log
  - after-change：退出码 0，通过 31，失败 0 · 日志 docs/logs/20261028-141338-after-change.log
  - module-tests：退出码 0，通过 12，失败 0 · 日志 docs/logs/20261028-141349-module-tests.log
  - baseline-target：退出码 1，通过 3，失败 14 · 日志 docs/logs/20261028-141333-baseline-target.log

## 5. 已失效 / 被取代的规格（1 条，已排除）
- ADR-0007 检索参数命名使用 min_price / max_price（superseded → 由 ADR-0011 取代） · 来源 `data/specs/ADR-0007-filter-param-naming.md`

## 6. 检索审计
- seekdb：run_id=1 · 耗时 150ms · 命中 57 行（全文 21，向量 36）
