"""规格变更测试：更新规格并重新导入后，上下文必须使用新内容。"""

from __future__ import annotations

from pathlib import Path

from sddctx.assemble import build_context
from sddctx.ingest import ingest

ROOT = Path(__file__).resolve().parents[1]
UPDATED = ROOT / "data" / "specs_updated"

NEW_CLAUSE = """
### FILTER-905 测试专用条款
- 状态: 生效
- 适用: GET /api/products
- 关联代码: workdir/biz/catalog/service.py#search_products
- 正文:
  - 这是一条只在测试中出现的条款，用于证明上下文来自数据库而不是硬编码。
"""


def test_updated_spec_changes_context(seeded_db, cfg):
    task = "为 GET /api/products 增加价格区间筛选，保持既有排序与分页行为"
    base_ids = {
        spec["clause_id"]
        for spec in build_context(db=seeded_db, project=cfg.project, task_text=task, persist=False)[
            "specs"
        ]
    }
    assert "FILTER-905" not in base_ids

    import shutil

    if UPDATED.exists():
        shutil.rmtree(UPDATED)
    shutil.copytree(ROOT / "data" / "specs", UPDATED)
    contract = UPDATED / "API-catalog-v2.md"
    contract.write_text(contract.read_text(encoding="utf-8") + NEW_CLAUSE, encoding="utf-8")

    try:
        ingest(cfg, seeded_db, reset=True, specs_dir=UPDATED)
        updated = build_context(db=seeded_db, project=cfg.project, task_text=task, persist=False)
        updated_ids = {spec["clause_id"] for spec in updated["specs"]}
        assert "FILTER-905" in updated_ids
        assert "FILTER-001" in updated_ids
        clause = next(spec for spec in updated["specs"] if spec["clause_id"] == "FILTER-905")
        assert clause["doc_path"].endswith("specs_updated/API-catalog-v2.md")
    finally:
        ingest(cfg, seeded_db, reset=True)
        shutil.rmtree(UPDATED, ignore_errors=True)

    restored = {
        spec["clause_id"]
        for spec in build_context(db=seeded_db, project=cfg.project, task_text=task, persist=False)[
            "specs"
        ]
    }
    assert "FILTER-905" not in restored
    assert restored == base_ids
