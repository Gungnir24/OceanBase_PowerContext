"""上下文契约测试：输出必须能定位「规格条目 / 代码符号 / 测试」，且来源真实存在。"""

from __future__ import annotations

from pathlib import Path

from sddctx.assemble import build_context, detect_endpoint

ROOT = Path(__file__).resolve().parents[1]

TASK = (
    "为 GET /api/products 增加按价格区间（price_min / price_max，单位元、闭区间）"
    "与库存状态（in_stock_only）的筛选条件，并保持既有响应结构、排序与分页行为不变。"
)

REQUIRED_SPECS = {"FILTER-001", "FILTER-002", "FILTER-003", "FILTER-004", "COMPAT-001", "ADR-0011"}
REQUIRED_CODE = {
    ("workdir/biz/catalog/service.py", "search_products"),
    ("workdir/biz/catalog/api.py", "handle_products_query"),
}


def test_endpoint_detection():
    assert detect_endpoint(TASK) == "/api/products"
    assert detect_endpoint("没有接口的普通描述") == ""


def test_context_contains_required_sources(seeded_db, cfg):
    context = build_context(
        db=seeded_db,
        project=cfg.project,
        task_text=TASK,
        persist=False,
    )

    spec_ids = {spec["clause_id"] for spec in context["specs"]}
    assert REQUIRED_SPECS <= spec_ids

    code_ids = {(item["path"], item["symbol"]) for item in context["code"]}
    assert REQUIRED_CODE <= code_ids

    test_paths = {item["path"] for item in context["tests"]}
    assert "workdir/tests/test_search_target.py" in test_paths
    assert "workdir/tests/test_search_regression.py" in test_paths

    # 来源必须真实存在于磁盘
    for path in {item["path"] for item in context["code"]} | test_paths:
        assert (ROOT / path).is_file(), path
    assert context["diagnostics"]["missing_refs"] == []


def test_superseded_decision_is_excluded_but_visible(seeded_db, cfg):
    context = build_context(db=seeded_db, project=cfg.project, task_text=TASK, persist=False)
    excluded = {spec["clause_id"]: spec for spec in context["excluded_specs"]}
    assert "ADR-0007" in excluded
    assert excluded["ADR-0007"]["status"] == "superseded"
    assert excluded["ADR-0007"]["replaced_by"] == "ADR-0011"
    assert "ADR-0007" not in {spec["clause_id"] for spec in context["specs"]}


def test_markdown_renders_all_sections(seeded_db, cfg):
    context = build_context(db=seeded_db, project=cfg.project, task_text=TASK, persist=False)
    markdown = context["markdown"]
    for heading in ("适用规格", "需要修改的代码", "验证用测试", "任务状态", "检索审计"):
        assert heading in markdown
    # 每条规格都要能回溯到文件
    for spec in context["specs"]:
        assert spec["doc_path"] in markdown


def test_budget_trims_semantic_entries_only(seeded_db, cfg):
    full = build_context(db=seeded_db, project=cfg.project, task_text=TASK, persist=False)
    trimmed = build_context(
        db=seeded_db,
        project=cfg.project,
        task_text=TASK,
        max_bytes=3000,
        persist=False,
    )
    # 规格与锚点条目永不因预算被丢弃
    assert {spec["clause_id"] for spec in trimmed["specs"]} == {
        spec["clause_id"] for spec in full["specs"]
    }
    assert len(trimmed["code"]) <= len(full["code"])
    if trimmed["diagnostics"]["truncated"]:
        assert trimmed["diagnostics"]["dropped"]
    assert trimmed["diagnostics"]["rendered_bytes"] <= len(trimmed["markdown"].encode("utf-8"))
