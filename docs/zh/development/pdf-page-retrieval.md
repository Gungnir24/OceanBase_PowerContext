# PDF 全文采集与页级证据检索

`powercontext.sources.pdf_pages` 接收完整 PDF 字节，使用环境中的 `pypdf` 按物理页顺序提取全部文本。空白页也形成页记录，物理页码从 1 开始，与正文中印刷的页码无关。

## 采集页面

```python
from powercontext.sources import (
    SourceDefinitionRegistry,
    manifest_for_definition,
    project_source_for_transport,
)
from powercontext.sources.pdf_pages import (
    PDF_PAGE_SOURCE_DEFINITION,
    PDFDocumentInput,
    parse_pdf_document,
)
from powercontext.builtin.runtime import SubmitSourceObservation

pages = parse_pdf_document(
    PDFDocumentInput(document_id="annual-report", content=original_pdf_bytes)
)
registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
await runtime.ingestion.register(manifest_for_definition(PDF_PAGE_SOURCE_DEFINITION))
for page in pages:
    source = await registry.resolve(page)
    observation = project_source_for_transport(registry, source)
    await runtime.ingestion.submit(
        SubmitSourceObservation(scope_id=scope_id, observation=observation)
    )
```

页面记录保留完整输入字节的 SHA-256、原始文档 ID、物理页码和该页文本。Source 为 `CAPTURED`；其身份由文档 ID、文档摘要、物理页码组成的 JSON 数组摘要确定。更新 PDF 字节会产生新的页面身份，已保存的旧引用仍指向原修订。

`powercontext.text-evidence/1` 投影正文为文档 ID、换行和完整页文本，metadata 仅包含 `document_id`、`document_sha256`、`page_number`。解析器接受能够用空密码打开的权限标记 PDF；损坏或无法完整读取的输入以 `ValueError` 失败。

## 搜索和回查

页面经配置的 Memory pipeline 处理后，用已打开的 scoped Memory 检索：

```python
from powercontext.builtin.runtime import GetMemoryEntryRequest
from powercontext.sources.pdf_pages import search_pdf_pages

memory = runtime.memory.for_scope(scope_id)
citations = await search_pdf_pages(memory, question, limit=20)
for citation in citations:
    entry = await memory.get(GetMemoryEntryRequest(citation=citation))
```

搜索仅通过传入 Memory 的公开 `search` 和 `get` 接口获取数据。它采用离线财务词项扩展、词频重排、文档名称匹配和章节覆盖，保留多报表问题涉及的不同证据类型，再验证真实 PDF Source 引用并按页去重。返回值最多包含 `limit` 个精确 `MemoryCitation`；空问题或零上限返回空 tuple，负上限抛出 `ValueError`。

实现不读取文件路径、测评资产、答案或证据页清单，不调用模型或在线服务。查询处理仅影响检索排序，不改写持久化的原始页面文本。

## 验证

在公开评测的 Linux、Python 3.12 固定依赖环境中运行：

```bash
make -C competition test-pdf
python -m pytest -q tests/test_pdf_pages.py tests/e2e/test_pdf_page_retrieval.py
```

产品测试还覆盖物理空白页、表格数字、权限 PDF、后续页损坏、修订身份、重复 Memory 条目、非 PDF 干扰、多文档名称、scope 隔离，以及更新后的旧引用回查。

公开包不提供质量 gold。公开自测可以验证全页采集、持久化和引用合同，不能据此计算或宣称正式评测的词项质量指标及 evidence-page Recall@20。
