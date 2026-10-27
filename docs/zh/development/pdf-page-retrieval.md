# PDF 全文采集与页级证据检索

`powercontext.sources.pdf_pages` 接收完整 PDF 字节，使用 `pypdf` 提取全部物理页，包括空白页。物理页码从 1 开始，与财报印刷页码无关。安装 PDF 可选依赖：

```bash
uv sync --locked --extra pdf
```

## 页面采集

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
    await runtime.ingestion.submit(
        SubmitSourceObservation(
            scope_id=scope_id,
            observation=project_source_for_transport(registry, source),
        )
    )
```

`PDFPageInput` 保存文档 ID、完整 PDF 字节的 SHA-256、物理页码和完整页文本。页记录与 `PDFPageSource` 均不可变；Source 的 `page` 字段保留原始页输入。Source 为 `CAPTURED`，身份由文档 ID、摘要、页码组成的 JSON 数组摘要确定。修改 PDF 字节会产生新的身份，历史引用仍指向原修订。

标准 `powercontext.text-evidence/1` 投影正文为 `DOCUMENT_ID\nPAGE_TEXT`，metadata 恰好包含 `document_id`、`document_sha256` 和 `page_number`。输入、页码和摘要均严格校验。无需用户密码、可用空密码打开的权限 PDF 可读取；损坏的输入、内容流、文本指令或无法完整解析的页树以 `ValueError` 原子失败。

PDF 页级证据的 Memory 正文不设单条字节上限，避免长页截断或分割改变原投影。其他来源证据保留 1 MiB 上限，无来源记忆保留 8 KiB 上限。写入、历史读取和内容哈希支持完整长页，审核证据另行按既有审核预算截取。

## 页级检索

```python
from powercontext.builtin.runtime import GetMemoryEntryRequest
from powercontext.sources.pdf_pages import search_pdf_pages

memory = runtime.memory.for_scope(scope_id)
citations = await search_pdf_pages(memory, question, limit=20)
for citation in citations:
    entry = await memory.get(GetMemoryEntryRequest(citation=citation))
```

`powercontext.sources.pdf_search` 通过当前 scoped Memory 的公开 `list` 读取可用 PDF 记录，通过 `search` 获取原生全文命中，再执行离线词项扩展、词频重排、相邻词组匹配、公司名称与缩写匹配，以及多报表覆盖。它按 PDF Source 身份去重并返回真实记录的精确 `MemoryCitation`，最多 `limit` 条。

每次调用重新读取当前记录，词项缓存只保存文本分析结果，不保存引用或 scope 状态。公共入口保留空问题和零上限返回空 tuple 的兼容行为；其他调用使用非空字符串问题及正整数上限。检索不读取文件路径、答案、gold 或测评资产，不调用模型及在线服务，不改写原始页文本。

## 验证

公开自测使用固定的 Python 3.12 和依赖环境：

```bash
make -C competition test-pdf
```

公开测试检查全部物理页、权限与损坏输入、真实持久化、重启和精确引用。公开包不提供质量 gold，公开自测通过不能代替官方的全文质量和 evidence-page Recall@20 验收。
