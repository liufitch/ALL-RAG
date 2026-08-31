# Document Parsing, Segmentation, and Preview Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为全部支持格式提供统一解析结果、普通/父子分段，并让预览 API 使用与正式索引完全相同的实现。

**Architecture:** 格式 parser 只负责把文件转换成带源定位的 `ParsedDocument`；splitter 只消费统一 block，不依赖文件库。`PreviewService` 从 MinIO 读取文件，调用 registry 和 splitter，限制时间与返回数量但不持久化分段。

**Tech Stack:** charset-normalizer、markdown-it-py、pypdf、python-docx、openpyxl、xlrd、Python csv、FastAPI、pytest。

**Spec:** `docs/superpowers/specs/2026-08-31-dify-style-dataset-indexing-design.md`

## Global Constraints

- 预览和 Celery 正式索引必须调用同一个 `ParserRegistry` 和 `Segmenter`。
- PDF 只支持文本层；无文本返回 `PDF_NO_EXTRACTABLE_TEXT`，不得假装成功。
- `.doc` 不在支持范围；`.docx` 不执行宏或外部对象。
- Excel/CSV 行文本必须带表头，metadata 必须带 sheet 和真实行号。
- 普通分段和父子分段使用字符长度，不把字符数命名为 token。
- 父子分段只生成父子结构；是否生成向量由后续索引计划决定。
- 预览不写 `document_segments`、不调用 Embedding、不连接 Milvus。

---

## File Structure

- Create: `rag_modules/parsing/models.py` — parsed document/block/warning/error。
- Create: `rag_modules/parsing/base.py` — parser protocol/context。
- Create: `rag_modules/parsing/registry.py` — extension-to-parser registry。
- Create: `rag_modules/parsing/text_parser.py` — TXT。
- Create: `rag_modules/parsing/markdown_parser.py` — Markdown。
- Create: `rag_modules/parsing/pdf_parser.py` — PDF text layer。
- Create: `rag_modules/parsing/docx_parser.py` — DOCX。
- Create: `rag_modules/parsing/tabular.py` — 公共表头、类型格式化和行转换。
- Create: `rag_modules/parsing/xlsx_parser.py` — XLSX。
- Create: `rag_modules/parsing/xls_parser.py` — XLS。
- Create: `rag_modules/parsing/csv_parser.py` — CSV。
- Create: `rag_modules/segmentation/models.py` — 配置和 preview segment 类型。
- Create: `rag_modules/segmentation/general.py` — 普通递归分段。
- Create: `rag_modules/segmentation/parent_child.py` — 父子分段。
- Create: `rag_modules/segmentation/service.py` — 统一 `Segmenter`。
- Create: `rag_modules/services/preview_service.py` — MinIO 读取和限制。
- Create: `rag_modules/api/dto/indexing_preview.py` — 请求/响应校验。
- Create: `rag_modules/api/indexing_preview_api.py` — preview route。
- Modify: `main.py` — 注册 preview router。
- Create: `tests/fixtures/documents/*` — 小型、无敏感信息的格式样例。
- Test: `tests/unit/parsing/`。
- Test: `tests/unit/segmentation/`。
- Test: `tests/unit/services/test_preview_service.py`。
- Test: `tests/api/test_indexing_preview_api.py`。

### Task 1: Unified Parser Types and Registry

**Interfaces:**
- Produces: `ParsedBlock(block_type, text, metadata)`。
- Produces: `ParsedDocument(document_id, filename, source_type, blocks, metadata, warnings)`。
- Produces: `ParserRegistry.parse(extension, stream, context) -> ParsedDocument`。
- Raises: `DocumentParseError(code, message, retryable=False)`。

- [ ] **Step 1: Write failing registry tests**

```python
# tests/unit/parsing/test_registry.py
def test_registry_dispatches_normalized_extension():
    parser = RecordingParser()
    registry = ParserRegistry({".md": parser})
    context = ParseContext(document_id="doc-1", filename="README.MD")

    parsed = registry.parse(".MD", io.BytesIO(b"body"), context)

    assert parsed.document_id == "doc-1"
    assert parser.calls[0].filename == "README.MD"


def test_registry_rejects_unknown_extension_with_stable_code():
    with pytest.raises(DocumentParseError) as error:
        ParserRegistry({}).parse(".doc", io.BytesIO(b"x"), ParseContext("d", "x.doc"))
    assert error.value.code == "UNSUPPORTED_FILE_TYPE"
```

- [ ] **Step 2: Run and confirm missing parser package**

Run: `python -m pytest tests/unit/parsing/test_registry.py -v`

Expected: FAIL on import.

- [ ] **Step 3: Implement immutable parser models and registry**

```python
@dataclass(frozen=True)
class ParserWarning:
    code: str
    message: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ParsedBlock:
    block_type: Literal["paragraph", "heading", "list_item", "code", "table_row"]
    text: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class ParsedDocument:
    document_id: str
    filename: str
    source_type: str
    blocks: tuple[ParsedBlock, ...]
    metadata: dict[str, Any]
    warnings: tuple[ParserWarning, ...] = ()
```

Registry must seek the stream to 0 before dispatch and reject an empty `blocks` result with `NO_EXTRACTABLE_TEXT` unless the parser already emitted a more specific error.

- [ ] **Step 4: Run registry tests**

Run: `python -m pytest tests/unit/parsing/test_registry.py -v`

Expected: PASS.

- [ ] **Step 5: Commit parser boundary**

```bash
git add rag_modules/parsing/models.py rag_modules/parsing/base.py rag_modules/parsing/registry.py tests/unit/parsing/test_registry.py
git commit -m "feat: add unified document parser boundary"
```

### Task 2: TXT and Markdown Parsers

**Interfaces:**
- Produces: `TextParser.parse(stream, context)`。
- Produces: `MarkdownParser.parse(stream, context)` with `heading_path` metadata。
- Consumes: unified parser models.

- [ ] **Step 1: Write failing text/Markdown behavior tests**

```python
# tests/unit/parsing/test_text_markdown.py
def test_text_parser_detects_encoding_and_preserves_source_lines():
    parsed = TextParser().parse(
        io.BytesIO("第一段\n\n第二段".encode("gb18030")),
        ParseContext("doc-1", "guide.txt"),
    )
    assert [block.text for block in parsed.blocks] == ["第一段", "第二段"]
    assert parsed.blocks[1].metadata == {"line_start": 3, "line_end": 3}


def test_markdown_parser_keeps_heading_path_and_code_block_atomic():
    source = b"# Install\n\nIntro\n\n```python\nprint('ok')\n```"
    parsed = MarkdownParser().parse(io.BytesIO(source), ParseContext("doc-1", "guide.md"))
    code = next(block for block in parsed.blocks if block.block_type == "code")
    assert code.text == "print('ok')\n"
    assert code.metadata["heading_path"] == ["Install"]
```

- [ ] **Step 2: Run and verify parser classes are missing**

Run: `python -m pytest tests/unit/parsing/test_text_markdown.py -v`

Expected: FAIL on imports.

- [ ] **Step 3: Implement decoding and Markdown token walking**

Create `decode_text(data: bytes) -> tuple[str, str]` that tries UTF-8-SIG first, then `charset_normalizer.from_bytes(data).best()`, and rejects low-confidence/chaotic results with `TEXT_ENCODING_UNCERTAIN`. Normalize CRLF/CR to LF, strip NUL, and split TXT on blank-line paragraph boundaries while tracking source line ranges.

Use `MarkdownIt("commonmark", {"html": False})` tokens to maintain a heading stack. Emit headings, paragraphs, list items, fenced code, and table rows as distinct blocks. Put link destinations and front matter in document metadata; never execute HTML or code.

- [ ] **Step 4: Run text/Markdown tests**

Run: `python -m pytest tests/unit/parsing/test_text_markdown.py -v`

Expected: PASS including UTF-8 BOM, GB18030, low-confidence binary rejection, headings, lists, code and tables.

- [ ] **Step 5: Commit text parsers**

```bash
git add rag_modules/parsing/text_parser.py rag_modules/parsing/markdown_parser.py tests/unit/parsing/test_text_markdown.py
git commit -m "feat: parse text and markdown documents"
```

### Task 3: PDF and DOCX Parsers

**Interfaces:**
- Produces: PDF paragraph blocks with `page` metadata。
- Produces: DOCX paragraph/table blocks with `heading_path` metadata。

- [ ] **Step 1: Add fixtures and failing parser tests**

```python
# tests/unit/parsing/test_pdf_docx.py
def test_pdf_parser_preserves_page_numbers(fixture_bytes):
    parsed = PdfParser().parse(
        io.BytesIO(fixture_bytes("two-pages.pdf")), ParseContext("doc-1", "two-pages.pdf")
    )
    assert {block.metadata["page"] for block in parsed.blocks} == {1, 2}


def test_scanned_pdf_reports_specific_error(fixture_bytes):
    with pytest.raises(DocumentParseError) as error:
        PdfParser().parse(io.BytesIO(fixture_bytes("image-only.pdf")), ParseContext("d", "scan.pdf"))
    assert error.value.code == "PDF_NO_EXTRACTABLE_TEXT"


def test_docx_preserves_heading_and_table_headers(fixture_bytes):
    parsed = DocxParser().parse(io.BytesIO(fixture_bytes("heading-table.docx")), ParseContext("d", "x.docx"))
    row = next(block for block in parsed.blocks if block.block_type == "table_row")
    assert row.text == "产品：A；价格：100"
    assert row.metadata["heading_path"] == ["产品表"]
```

- [ ] **Step 2: Run and observe missing implementations**

Run: `python -m pytest tests/unit/parsing/test_pdf_docx.py -v`

Expected: FAIL on missing `PdfParser`/`DocxParser`.

- [ ] **Step 3: Implement bounded PDF and DOCX parsing**

PDF: construct `PdfReader`, reject encryption that cannot be decrypted, enforce `settings.parser.max_pdf_pages`, extract per page, normalize common line-wrap artifacts, emit `PDF_EMPTY_PAGE` warnings, and raise `PDF_NO_EXTRACTABLE_TEXT` when all pages are empty.

DOCX: use `Document(stream)`, iterate body XML children so paragraphs and tables retain source order, update heading path from `Heading 1`–`Heading 6`, flatten table rows through the shared tabular formatter, and skip headers/footers/images.

- [ ] **Step 4: Run format tests**

Run: `python -m pytest tests/unit/parsing/test_pdf_docx.py -v`

Expected: PASS for text PDF, image-only PDF, encrypted/corrupt PDF, headings, lists and DOCX tables.

- [ ] **Step 5: Commit PDF/DOCX support**

```bash
git add rag_modules/parsing/pdf_parser.py rag_modules/parsing/docx_parser.py tests/fixtures/documents tests/unit/parsing/test_pdf_docx.py
git commit -m "feat: parse pdf and docx documents"
```

### Task 4: XLS, XLSX, and CSV Parsers

**Interfaces:**
- Produces: `format_table_row(headers, values) -> str`。
- Produces: table row metadata `sheet`, `row`, `headers`。
- CSV uses logical sheet name `CSV`.

- [ ] **Step 1: Write failing tabular tests**

```python
# tests/unit/parsing/test_tabular.py
@pytest.mark.parametrize("fixture_name", ["orders.xls", "orders.xlsx", "orders.csv"])
def test_tabular_parsers_emit_self_describing_rows(fixture_name, parser_registry, fixture_bytes):
    parsed = parser_registry.parse(
        Path(fixture_name).suffix, io.BytesIO(fixture_bytes(fixture_name)), ParseContext("d", fixture_name)
    )
    row = next(block for block in parsed.blocks if block.block_type == "table_row")
    assert row.text == "订单号：A001；客户：张三；金额：100"
    assert row.metadata["row"] == 2
    assert row.metadata["headers"] == ["订单号", "客户", "金额"]


def test_xlsx_uses_sheet_name_and_skips_hidden_sheet(parser_registry, fixture_bytes):
    parsed = parser_registry.parse(".xlsx", io.BytesIO(fixture_bytes("multi-sheet.xlsx")), ParseContext("d", "x.xlsx"))
    assert {block.metadata["sheet"] for block in parsed.blocks} == {"订单", "客户"}
    assert any(warning.code == "HIDDEN_SHEET_SKIPPED" for warning in parsed.warnings)
```

- [ ] **Step 2: Run and verify missing parsers**

Run: `python -m pytest tests/unit/parsing/test_tabular.py -v`

Expected: FAIL on missing tabular modules.

- [ ] **Step 3: Implement shared tabular normalization and bounded readers**

Format dates as ISO strings, booleans as `true`/`false`, integers without `.0`, and skip null cells while retaining header alignment. Detect the first non-empty row as headers, deduplicate blank/duplicate headers into stable names, and enforce configured row/column/cell limits.

Use `openpyxl.load_workbook(stream, read_only=True, data_only=False)` for XLSX, `xlrd.open_workbook(file_contents=stream.read(), on_demand=True)` for XLS, and Python `csv` with `csv.Sniffer` plus the shared text decoder for CSV. CSV metadata uses `sheet="CSV"` and true source line numbers.

- [ ] **Step 4: Run tabular tests**

Run: `python -m pytest tests/unit/parsing/test_tabular.py -v`

Expected: PASS for multiple sheets, hidden/empty sheets, dates, formulas without cached values, quoted CSV newlines and semicolon delimiters.

- [ ] **Step 5: Commit tabular parsers**

```bash
git add rag_modules/parsing/tabular.py rag_modules/parsing/xlsx_parser.py rag_modules/parsing/xls_parser.py rag_modules/parsing/csv_parser.py tests/fixtures/documents tests/unit/parsing/test_tabular.py
git commit -m "feat: parse spreadsheet and csv documents"
```

### Task 5: General and Parent-Child Segmenters

**Interfaces:**
- Produces: `Segmenter.segment(parsed, config) -> SegmentationResult`。
- Produces: `PreviewSegment(local_id, parent_local_id, position, content, source_metadata, index_type)`。
- Consumes: discriminated union `GeneralSegmentationConfig | ParentChildSegmentationConfig`。

- [ ] **Step 1: Write failing segmentation contracts**

```python
# tests/unit/segmentation/test_segmenters.py
def test_general_segmentation_honors_character_limit_and_overlap():
    parsed = parsed_text("第一句。第二句。第三句。", block_type="paragraph")
    result = Segmenter().segment(parsed, GeneralSegmentationConfig(max_chunk_length=8, overlap=2))
    assert all(0 < len(item.content) <= 8 for item in result.segments)
    assert result.segments[0].content[-2:] == result.segments[1].content[:2]


def test_parent_child_emits_parent_before_children_and_links_them():
    parsed = parsed_text("第一段很长。第二句继续。", block_type="paragraph")
    result = Segmenter().segment(
        parsed,
        ParentChildSegmentationConfig(parent_mode="paragraph", parent_max_length=20, child_max_length=8, child_overlap=1),
    )
    parent = next(item for item in result.segments if item.index_type == "parent")
    children = [item for item in result.segments if item.parent_local_id == parent.local_id]
    assert children
    assert all(item.index_type == "child" for item in children)
```

- [ ] **Step 2: Run and observe missing segmenters**

Run: `python -m pytest tests/unit/segmentation/test_segmenters.py -v`

Expected: FAIL on missing segmentation package.

- [ ] **Step 3: Implement deterministic recursive splitting**

Use boundary order: configured separator, `\n\n`, `\n`, Chinese sentence punctuation, English sentence punctuation, whitespace, hard character split. Validate `0 <= overlap < max_chunk_length`. Preserve merged source metadata ranges. Treat code/table rows as atomic until they exceed the hard maximum.

For parent-child mode, generate deterministic local IDs such as `p-000001` and `c-000001`, parent rows before their children, and full-document fallback warnings when a parent exceeds the configured limit. For spreadsheets, group consecutive table rows under a sheet/row-group parent and keep headers in every child.

- [ ] **Step 4: Run all segmentation tests**

Run: `python -m pytest tests/unit/segmentation -v`

Expected: PASS for Chinese/English boundaries, hard split, overlap, code, tables, parent links and empty content.

- [ ] **Step 5: Commit segmentation**

```bash
git add rag_modules/segmentation tests/unit/segmentation
git commit -m "feat: add general and parent child segmentation"
```

### Task 6: Real Preview Service and API

**Interfaces:**
- Produces: `PreviewService.preview(dataset_id, document_ids, request) -> PreviewResponse`。
- Produces: `POST /api/knowledge_base/{dataset_id}/indexing/preview`。
- Consumes: `DocumentRepository`, `ObjectStorage`, `ParserRegistry`, `Segmenter`。

- [ ] **Step 1: Write failing service and route tests**

```python
# tests/unit/services/test_preview_service.py
@pytest.mark.asyncio
async def test_preview_uses_real_parser_and_truncates_response():
    service = make_preview_service(max_chunks=2, stored_text="A。B。C。D。")
    response = await service.preview("dataset-1", ["doc-1"], general_request(max_length=2))
    assert response.total_chunks == 4
    assert len(response.chunks) == 2
    assert response.truncated is True
    assert service.segmenter.calls == 1


# tests/api/test_indexing_preview_api.py
def test_parent_child_with_economy_is_rejected(client):
    response = client.post(
        "/api/knowledge_base/dataset-1/indexing/preview",
        json={"document_ids":["doc-1"], "indexing_technique":"economy", "segmentation":{"mode":"parent_child"}},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "PARENT_CHILD_REQUIRES_HIGH_QUALITY"
```

- [ ] **Step 2: Run and confirm route/service absence**

Run: `python -m pytest tests/unit/services/test_preview_service.py tests/api/test_indexing_preview_api.py -v`

Expected: FAIL with imports/404.

- [ ] **Step 3: Implement preview orchestration and validation**

Validate ownership of all document IDs in one repository query. Reject deleted documents, too many documents, over-limit files, parent-child/economy, invalid overlaps and unknown models. Run synchronous parser and splitter work through `anyio.to_thread.run_sync` so PDF/Office processing does not block the FastAPI event loop, and apply `anyio.fail_after(settings.preview.timeout_seconds)` with abandoned late results. Accumulate complete counts but return at most `max_chunks`; serialize parser warnings and source metadata. Do not call any segment repository or vector provider.

- [ ] **Step 4: Run preview and parser regression tests**

Run: `python -m pytest tests/unit/parsing tests/unit/segmentation tests/unit/services/test_preview_service.py tests/api/test_indexing_preview_api.py -v`

Expected: PASS; spies prove zero DB segment writes and zero Embedding/Milvus calls.

- [ ] **Step 5: Commit preview API**

```bash
git add rag_modules/services/preview_service.py rag_modules/api/dto/indexing_preview.py rag_modules/api/indexing_preview_api.py main.py tests/unit/services/test_preview_service.py tests/api/test_indexing_preview_api.py
git commit -m "feat: preview real document segments"
```

## Phase Verification

Run:

```bash
python -m pytest tests/unit/parsing tests/unit/segmentation tests/unit/services/test_preview_service.py tests/api/test_indexing_preview_api.py -v
git diff --check
```

Expected: every supported extension parses into the unified model, preview and future indexing share the same registry/segmenter, and preview causes no persistent or vector writes.
