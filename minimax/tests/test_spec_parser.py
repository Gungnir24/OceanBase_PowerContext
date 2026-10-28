"""规格解析的离线测试：不依赖数据库，验证「条目化」契约本身。"""

from __future__ import annotations

from pathlib import Path

import pytest

from sddctx.specs import load_specs

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def docs():
    return load_specs(ROOT / "data" / "specs", ROOT)


def test_all_spec_files_are_parsed(docs):
    assert len(docs) == 5
    ids = {doc.doc_id for doc in docs}
    assert ids == {"api-catalog", "srs-catalog", "test-plan", "adr-0007", "adr-0011"}


def test_contract_clauses_are_complete(docs):
    contract = next(doc for doc in docs if doc.doc_id == "api-catalog")
    clause_ids = {clause.clause_id for clause in contract.clauses}
    for expected in ("COMPAT-001", "COMPAT-002", "COMPAT-003", "COMPAT-004"):
        assert expected in clause_ids
    for expected in ("FILTER-001", "FILTER-002", "FILTER-003", "FILTER-004"):
        assert expected in clause_ids
    assert contract.version == "2.3.0"


def test_superseded_decision_is_marked(docs):
    adr = next(doc for doc in docs if doc.doc_id == "adr-0007")
    clause = adr.clauses[0]
    assert clause.status == "superseded"
    assert clause.replaced_by == "ADR-0011"


def test_clauses_declare_real_anchors(docs):
    anchors = []
    for doc in docs:
        for clause in doc.clauses:
            if clause.clause_id.startswith(("FILTER", "COMPAT", "ADR-0011")):
                anchors.extend(clause.code_refs)
                anchors.extend(clause.test_refs)
    assert anchors, "生效条目必须声明关联代码/测试，否则上下文无法定位来源"
    for ref in anchors:
        path = ref.split("#", 1)[0]
        assert (ROOT / path).is_file(), f"规格里声明的路径不存在：{path}"


def test_body_text_is_kept(docs):
    contract = next(doc for doc in docs if doc.doc_id == "api-catalog")
    filter_clause = next(clause for clause in contract.clauses if clause.clause_id == "FILTER-001")
    assert "闭区间" in filter_clause.body
    assert "两位小数" in filter_clause.body
