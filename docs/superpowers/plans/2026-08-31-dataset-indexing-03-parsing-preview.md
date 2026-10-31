# 文档解析、分段与预览实施计划

> **面向智能体工作者：** 必需子技能：使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans`，按任务逐项执行本计划。步骤使用复选框（`- [ ]`）语法进行跟踪。

**目标：** 为全部支持格式提供统一解析结果、普通/父子分段，并让预览 API 使用与正式索引完全相同的实现。

**架构：** 格式 parser 只负责把文件转换成带源定位的 `ParsedDocument`；splitter 只消费统一 block，不依赖文件库。`PreviewService` 从 MinIO 读取文件，调用 registry 和 splitter，限制时间与返回数量但不持久化分段。

**技术栈：** charset-normalizer、markdown-it-py、pypdf、python-docx、openpyxl、xlrd、Python csv、FastAPI、pytest。

**规格：** `docs/superpowers/specs/2026-08-31-dify-style-dataset-indexing-design.md`

## 全局约束

- 预览和 Celery 正式索引必须调用同一个 `ParserRegistry` 和 `Segmenter`。
- PDF 只支持文本层；无文本返回 `PDF_NO_EXTRACTABLE_TEXT`，不得假装成功。
- `.doc` 不在支持范围；`.docx` 不执行宏或外部对象。
- Excel/CSV 行文本必须带表头，metadata 必须带 sheet 和真实行号。
- 普通分段和父子分段使用字符长度，不把字符数命名为 token。
- 父子分段只生成父子结构；是否生成向量由后续索引计划决定。
- 预览不写 `document_segments`、不调用 Embedding、不连接 Milvus。

---

## 文件结构

- 创建: `rag_modules/parsing/models.py` — parsed document/block/warning/error。
- 创建: `rag_modules/parsing/base.py` — parser protocol/context。
- 创建: `rag_modules/parsing/registry.py` — extension-to-parser registry。
- 创建: `rag_modules/parsing/text_parser.py` — TXT。
- 创建: `rag_modules/parsing/markdown_parser.py` — Markdown。
- 创建: `rag_modules/parsing/pdf_parser.py` — PDF text layer。
- 创建: `rag_modules/parsing/docx_parser.py` — DOCX。
- 创建: `rag_modules/parsing/tabular.py` — 公共表头、类型格式化和行转换。
- 创建: `rag_modules/parsing/xlsx_parser.py` — XLSX。
- 创建: `rag_modules/parsing/xls_parser.py` — XLS。
- 创建: `rag_modules/parsing/csv_parser.py` — CSV。
- 创建: `rag_modules/segmentation/models.py` — 配置和 preview segment 类型。
- 创建: `rag_modules/segmentation/general.py` — 普通递归分段。
- 创建: `rag_modules/segmentation/parent_child.py` — 父子分段。
- 创建: `rag_modules/segmentation/service.py` — 统一 `Segmenter`。
- 创建: `rag_modules/services/preview_service.py` — MinIO 读取和限制。
- 创建: `rag_modules/api/dto/indexing_preview.py` — 请求/响应校验。
- 创建: `rag_modules/api/indexing_preview_api.py` — preview route。
- 修改: `main.py` — 注册 preview router。
- 创建: `tests/fixtures/documents/*` — 小型、无敏感信息的格式样例。
- 测试: `tests/unit/parsing/`。
- 测试: `tests/unit/segmentation/`。
- 测试: `tests/unit/services/test_preview_service.py`。
- 测试: `tests/api/test_indexing_preview_api.py`。

### 任务 1：统一 Parser 类型与 Registry

**接口：**
- 产出: `ParsedBlock(block_type, text, metadata)`。
- 产出: `ParsedDocument(document_id, filename, source_type, blocks, metadata, warnings)`。
- 产出: `ParserRegistry.parse(extension, stream, context) -> ParsedDocument`。
- 抛出: `DocumentParseError(code, message, retryable=False)`。

- [ ] **步骤 1：编写失败的 Registry 测试**

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

- [ ] **步骤 2：运行并确认 Parser 包缺失**

运行: `python -m pytest tests/unit/parsing/test_registry.py -v`

预期：导入时失败。

- [ ] **步骤 3：实现不可变 Parser 模型与 Registry**

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

Registry 分发前必须将流 seek 到 0；若 `blocks` 结果为空，则应返回 `NO_EXTRACTABLE_TEXT`，除非 Parser 已给出更具体的错误。

- [ ] **步骤 4：运行 Registry 测试**

运行: `python -m pytest tests/unit/parsing/test_registry.py -v`

预期：通过。

- [ ] **步骤 5：提交 Parser 边界**

```bash
git add rag_modules/parsing/models.py rag_modules/parsing/base.py rag_modules/parsing/registry.py tests/unit/parsing/test_registry.py
git commit -m "feat: add unified document parser boundary"
```

### 任务 2：TXT 与 Markdown Parser

**接口：**
- 产出: `TextParser.parse(stream, context)`。
- 产出: `MarkdownParser.parse(stream, context)` with `heading_path` metadata。
- 依赖: unified parser models.

- [ ] **步骤 1：编写失败的文本/Markdown 行为测试**

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

- [ ] **步骤 2：运行并验证 Parser 类缺失**

运行: `python -m pytest tests/unit/parsing/test_text_markdown.py -v`

预期：导入失败。

- [ ] **步骤 3：实现解码与 Markdown token 遍历**

创建 `decode_text(data: bytes) -> tuple[str, str]`，先尝试 UTF-8-SIG，再尝试 `charset_normalizer.from_bytes(data).best()`；对于置信度低或内容混乱的结果，使用 `TEXT_ENCODING_UNCERTAIN` 拒绝。将 CRLF/CR 规范为 LF，移除 NUL，并按空行段落边界拆分 TXT，同时跟踪源行范围。

使用 `MarkdownIt("commonmark", {"html": False})` token 维护标题栈。将标题、段落、列表项、围栏代码和表格行分别输出为独立 block。将链接目标和 front matter 放入文档 metadata；绝不执行 HTML 或代码。

- [ ] **步骤 4：运行文本/Markdown 测试**

运行: `python -m pytest tests/unit/parsing/test_text_markdown.py -v`

预期：UTF-8 BOM、GB18030、低置信度二进制拒绝、标题、列表、代码和表格等测试均通过。

- [ ] **步骤 5：提交文本 Parser**

```bash
git add rag_modules/parsing/text_parser.py rag_modules/parsing/markdown_parser.py tests/unit/parsing/test_text_markdown.py
git commit -m "feat: parse text and markdown documents"
```

### 任务 3：PDF 与 DOCX Parser

**接口：**
- 产出: PDF paragraph blocks with `page` metadata。
- 产出: DOCX paragraph/table blocks with `heading_path` metadata。

- [ ] **步骤 1：添加 fixture 和失败的 Parser 测试**

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

- [ ] **步骤 2：运行并观察实现缺失**

运行: `python -m pytest tests/unit/parsing/test_pdf_docx.py -v`

预期：因缺少 `PdfParser`/`DocxParser` 而失败。

- [ ] **步骤 3：实现有界 PDF 和 DOCX 解析**

PDF：构造 `PdfReader`，拒绝无法解密的加密文件，执行 `settings.parser.max_pdf_pages` 限制，逐页提取文本，规范常见的换行伪影，发出 `PDF_EMPTY_PAGE` 警告，并在所有页面均为空时抛出 `PDF_NO_EXTRACTABLE_TEXT`。

DOCX：使用 `Document(stream)`，遍历 body XML 子元素以保留段落和表格的源顺序；根据 `Heading 1`–`Heading 6` 更新标题路径；通过共享表格格式化器展开表格行，并跳过页眉/页脚/图像。

- [ ] **步骤 4：运行格式测试**

运行: `python -m pytest tests/unit/parsing/test_pdf_docx.py -v`

预期：文本 PDF、纯图像 PDF、加密/损坏 PDF、标题、列表和 DOCX 表格测试均通过。

- [ ] **步骤 5：提交 PDF/DOCX 支持**

```bash
git add rag_modules/parsing/pdf_parser.py rag_modules/parsing/docx_parser.py tests/fixtures/documents tests/unit/parsing/test_pdf_docx.py
git commit -m "feat: parse pdf and docx documents"
```

### 任务 4：XLS、XLSX 与 CSV Parser

**接口：**
- 产出: `format_table_row(headers, values) -> str`。
- 产出: table row metadata `sheet`, `row`, `headers`。
- CSV 使用逻辑工作表名称 `CSV`。

- [ ] **步骤 1： Write failing tabular tests**

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

- [ ] **步骤 2：运行测试并确认解析器缺失**

运行: `python -m pytest tests/unit/parsing/test_tabular.py -v`

预期：因表格解析模块缺失而失败。

- [ ] **步骤 3：实现共享表格规范化逻辑和有界读取器**

日期格式化为 ISO 字符串，布尔值格式化为 `true`/`false`，整数不附加 `.0`。跳过空单元格时仍需保持表头对齐。将首个非空行识别为表头，把空表头和重复表头转换为稳定且不重复的名称，并强制执行已配置的行数、列数和单元格数量限制。

XLSX 使用 `openpyxl.load_workbook(stream, read_only=True, data_only=False)`；XLS 使用 `xlrd.open_workbook(file_contents=stream.read(), on_demand=True)`；CSV 使用 Python `csv`、`csv.Sniffer` 和共享文本解码器。CSV 元数据使用 `sheet="CSV"`，并记录真实源行号。

- [ ] **步骤 4： Run tabular tests**

运行: `python -m pytest tests/unit/parsing/test_tabular.py -v`

预期：多工作表、隐藏/空工作表、日期、无缓存值的公式、带引号的 CSV 换行以及分号分隔符测试均通过。

- [ ] **步骤 5： Commit tabular parsers**

```bash
git add rag_modules/parsing/tabular.py rag_modules/parsing/xlsx_parser.py rag_modules/parsing/xls_parser.py rag_modules/parsing/csv_parser.py tests/fixtures/documents tests/unit/parsing/test_tabular.py
git commit -m "feat: parse spreadsheet and csv documents"
```

### 任务 5：普通与父子分段器

**接口：**
- 产出: `Segmenter.segment(parsed, config) -> SegmentationResult`。
- 产出: `PreviewSegment(local_id, parent_local_id, position, content, source_metadata, index_type)`。
- 依赖: discriminated union `GeneralSegmentationConfig | ParentChildSegmentationConfig`。

- [ ] **步骤 1： Write failing segmentation contracts**

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

- [ ] **步骤 2： Run and observe missing segmenters**

运行: `python -m pytest tests/unit/segmentation/test_segmenters.py -v`

预期: FAIL on missing segmentation package.

- [ ] **步骤 3： Implement deterministic recursive splitting**

边界选择顺序为：配置的分隔符、`\n\n`、`\n`、中文句末标点、英文句末标点、空白字符，最后才按字符硬切分。校验 `0 <= overlap < max_chunk_length`。保留合并后的源元数据范围。代码块和表格行在超过硬性上限前视为不可拆分的整体。

父子模式需生成确定性的本地 ID，例如 `p-000001` 和 `c-000001`；先输出父行，再输出其子行。父块超过配置上限时，使用全文回退并产生 warning。对于电子表格，将连续表格行归入对应工作表/行组父块，并在每个子块中保留表头。

- [ ] **步骤 4： Run all segmentation tests**

运行: `python -m pytest tests/unit/segmentation -v`

预期：中英文边界、硬切分、重叠、代码、表格、父子链接和空内容测试均通过。

- [ ] **步骤 5： Commit segmentation**

```bash
git add rag_modules/segmentation tests/unit/segmentation
git commit -m "feat: add general and parent child segmentation"
```

### 任务 6：真实预览服务与 API

**接口：**
- 产出: `PreviewService.preview(dataset_id, document_ids, request) -> PreviewResponse`。
- 产出: `POST /api/knowledge_base/{dataset_id}/indexing/preview`。
- 依赖: `DocumentRepository`, `ObjectStorage`, `ParserRegistry`, `Segmenter`。

- [ ] **步骤 1： Write failing service and route tests**

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

- [ ] **步骤 2： Run and confirm route/service absence**

运行: `python -m pytest tests/unit/services/test_preview_service.py tests/api/test_indexing_preview_api.py -v`

预期：因导入缺失或路由返回 404 而失败。

- [ ] **步骤 3： Implement preview orchestration and validation**

通过一次 repository 查询校验所有文档 ID 的归属。拒绝已删除文档、文档数量过多、文件超限、父子模式与经济模式组合、无效重叠值和未知模型。使用 `anyio.to_thread.run_sync` 执行同步解析与分段，避免 PDF/Office 处理阻塞 FastAPI 事件循环；并通过 `anyio.fail_after(settings.preview.timeout_seconds)` 设置超时，丢弃超时后才返回的结果。统计完整数量，但最多返回 `max_chunks` 个分段；序列化 parser warning 和源元数据。不得调用分段 repository 或向量 provider。

- [ ] **步骤 4： Run preview and parser regression tests**

运行: `python -m pytest tests/unit/parsing tests/unit/segmentation tests/unit/services/test_preview_service.py tests/api/test_indexing_preview_api.py -v`

预期：测试通过；spy 应证明没有写入数据库分段，也没有调用 Embedding/Milvus。

- [ ] **步骤 5： Commit preview API**

```bash
git add rag_modules/services/preview_service.py rag_modules/api/dto/indexing_preview.py rag_modules/api/indexing_preview_api.py main.py tests/unit/services/test_preview_service.py tests/api/test_indexing_preview_api.py
git commit -m "feat: preview real document segments"
```

## 阶段验证

运行:

```bash
python -m pytest tests/unit/parsing tests/unit/segmentation tests/unit/services/test_preview_service.py tests/api/test_indexing_preview_api.py -v
git diff --check
```

预期：所有支持的扩展名都能解析为统一模型；预览与后续正式索引共用同一 registry/segmenter；预览不会产生持久化写入或向量写入。
