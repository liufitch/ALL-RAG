# PaddleOCR 文档解析与清洗实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有统一解析、预览、索引和分段修订流程中增加 PaddleOCR 图片与扫描版 PDF 解析，并提供标准自动清洗和分段后的手动编辑支持。

**Architecture:** 以 `OcrEngine` 协议隔离 PaddleOCR，真实适配器延迟导入可选依赖，测试通过 fake engine 注入。图片解析器直接处理已验证的图像；PDF 逐页优先读取文本层，仅把没有文本的页面交给可注入的页面渲染器和 OCR。OCR 输出先经过固定清洗，再生成现有 `ParsedDocument`，沿用 `ParserRegistry`、`PreviewService`、索引引擎和 `DocumentRevisionService`。

**Tech Stack:** Python 3.11、Pydantic Settings、pypdf、Pillow、可选 PaddleOCR/PaddlePaddle、现有 FastAPI/Celery/React 流程、pytest。

**Spec:** `docs/superpowers/specs/2026-10-24-paddleocr-document-processing-design.md`

## 最终实现与验证记录（2026-10-24）

功能实现已落地：图片和空文本 PDF 页面 OCR、标准清洗、来源元数据、统一预览与索引，以及复用现有分段编辑器保存 revision。下方保留原始实施步骤作为设计与测试参考。

- 后端完整回归：`PYTHONPATH=. .venv/bin/pytest -q`，**664 passed, 3 skipped**。
- 前端：`npm test`，**17 passed**；`npm run build` 成功。
- `python3 -m compileall -q rag_modules` 与 `git diff --check` 通过。
- 独立代码审查后补充并修复了 BGR 输入、NumPy 边界框、识别结果与边界框对齐、页边缘重复文本过滤、整份文档 OCR 行数预算及共享模型实例的回归用例；复审未发现尚未解决的重要问题。
- 自动化测试使用模拟 OCR/解码/渲染边界，并使用真实 NumPy 验证数组适配。后续在 Docker Desktop `linux/amd64` CPU 容器中安装并验证 PaddleOCR 3.7.0、PaddlePaddle 3.2.2、Pillow 12.3.0、pypdfium2 5.14.0；PP-OCRv5 成功识别合成中英文 PNG 和扫描 PDF，清洗正文、页码、置信度及文本框元数据符合预期。GPU 和生产文档识别效果尚未验证。
- 改动保留在工作区，未提交 Git。

## Global Constraints

- OCR 依赖必须是可选依赖，基础文本、Office 和文本层 PDF 流程不能因为未安装 PaddleOCR 而失效。
- 默认设备为 CPU；GPU 只能通过配置选择，API 进程和 Worker 必须读取同一 OCR 配置。
- 有文本层的 PDF 页面不得调用 OCR；只有空文本页面进入渲染和 OCR。
- 支持 `.png`、`.jpg`、`.jpeg`，上传必须同时校验 MIME、签名、大小和图像可解码性。
- OCR 自动清洗包括控制字符和空白规范化、置信度过滤、相邻行合并、重复页眉页脚过滤；不改写低置信度正文。
- 手动清洗发生在分段后，复用现有 `SegmentEditor` 和不可变 revision；不得覆盖原始对象。
- 单元测试不得下载真实模型、要求 GPU、连接外部 MinIO/Milvus/RabbitMQ 或依赖真实 PaddleOCR 初始化。
- 所有对外错误、warning 和 metadata 必须使用稳定、安全字段，不能泄漏第三方异常文本、模型路径或原始 OCR 噪声。
- 每个实现任务遵循“先写失败测试、确认失败、最小实现、确认通过”的 TDD 顺序。

---

### Task 1: OCR 结果契约与标准清洗

**Files:**
- Create: `rag_modules/parsing/ocr.py`
- Create: `rag_modules/parsing/ocr_cleaning.py`
- Modify: `rag_modules/parsing/models.py` only if a shared metadata type is needed; retain existing public dataclasses
- Test: `tests/unit/parsing/test_ocr_cleaning.py`
- Test: `tests/unit/parsing/test_ocr_engine.py`

**Interfaces:**
- Produces `OcrLine(text: str, confidence: float, box: tuple[tuple[float, float], ...] | None)`, `OcrEngine.recognize(image: object) -> tuple[OcrLine, ...]`, and `clean_ocr_lines(lines, *, page_number, settings, repeated_lines) -> CleanedOcrPage`.
- `CleanedOcrPage` exposes cleaned paragraph strings, retained line metadata, filtered-line count, and a repeated-header/footer removal flag for image/PDF parsers.
- `PaddleOcrEngine` is importable without PaddleOCR installed; construction or recognition raises only stable `DocumentParseError` codes defined in the spec.

- [ ] **Step 1: Write the failing cleaning tests**

```python
def test_clean_ocr_lines_normalizes_controls_filters_low_confidence_and_merges_lines():
    lines = (
        OcrLine("  第一行\x00  ", 0.98, ((0, 0), (10, 0), (10, 10), (0, 10))),
        OcrLine("第二行", 0.96, None),
        OcrLine("噪声!!!", 0.20, None),
    )
    cleaned = clean_ocr_lines(lines, page_number=2, settings=ParserSettings(), repeated_lines=set())
    assert [block.text for block in cleaned.blocks] == ["第一行 第二行"]
    assert cleaned.filtered_line_count == 1
    assert cleaned.blocks[0].metadata["page"] == 2

def test_clean_ocr_lines_removes_repeated_short_header_and_footer():
    lines = (OcrLine("项目内部资料", 0.99, None), OcrLine("正文内容", 0.99, None))
    cleaned = clean_ocr_lines(lines, page_number=3, settings=ParserSettings(), repeated_lines={"项目内部资料"})
    assert [block.text for block in cleaned.blocks] == ["正文内容"]
    assert cleaned.repeated_header_footer_removed is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest -q tests/unit/parsing/test_ocr_cleaning.py`

Expected: FAIL because the OCR result and cleaner interfaces do not exist.

- [ ] **Step 3: Write the failing engine adapter tests**

```python
def test_paddle_engine_reports_missing_optional_dependency(monkeypatch):
    monkeypatch.setitem(sys.modules, "paddleocr", None)
    engine = PaddleOcrEngine(device="cpu")
    with pytest.raises(DocumentParseError) as error:
        engine.recognize(object())
    assert error.value.code == "OCR_DEPENDENCY_MISSING"

def test_paddle_engine_adapts_common_ocr_result_shape(monkeypatch):
    fake = types.SimpleNamespace(PaddleOCR=lambda **kwargs: FakePaddleBackend())
    monkeypatch.setitem(sys.modules, "paddleocr", fake)
    lines = PaddleOcrEngine(device="cpu").recognize(object())
    assert lines == (OcrLine("标题", 0.97, ((1.0, 2.0), (3.0, 2.0), (3.0, 4.0), (1.0, 4.0))),)
```

- [ ] **Step 4: Run engine tests to verify they fail**

Run: `.venv/bin/pytest -q tests/unit/parsing/test_ocr_engine.py`

Expected: FAIL because `PaddleOcrEngine` is not implemented.

- [ ] **Step 5: Implement the minimal cleaner and lazy adapter**

Implement `OcrLine`, the protocol, stable exception mapping, result-shape normalization for common PaddleOCR `ocr()`/`predict()` responses, and deterministic cleaning. Keep the PaddleOCR import inside engine initialization, clamp confidence and line-count behavior from `ParserSettings`, and return only safe metadata.

- [ ] **Step 6: Run both test files**

Run: `.venv/bin/pytest -q tests/unit/parsing/test_ocr_cleaning.py tests/unit/parsing/test_ocr_engine.py`

Expected: PASS.

- [ ] **Step 7: Run `git diff --check` and record the task as complete**

Run: `git diff --check`

Expected: no whitespace errors. Keep changes in the working tree because this environment cannot create `.git/index.lock`.

### Task 2: Configuration, image parser, and upload validation

**Files:**
- Create: `rag_modules/parsing/image_parser.py`
- Modify: `rag_modules/config/settings.py`
- Modify: `rag_modules/upload_formats.py`
- Modify: `rag_modules/documents/validation.py`
- Modify: `pyproject.toml`
- Modify: `.env.example`
- Test: `tests/unit/parsing/test_image_parser.py`
- Test: `tests/unit/documents/test_upload_validation.py`
- Test: `tests/unit/config/test_settings.py`

**Interfaces:**
- `ParserSettings` gains bounded OCR fields: `ocr_enabled`, `ocr_device`, `ocr_language`, `ocr_model_dir`, `ocr_min_confidence`, `ocr_max_image_width`, `ocr_max_image_height`, `ocr_max_image_pixels`, and `ocr_max_lines_per_document`.
- `ImageParser(engine: OcrEngine | None = None)` implements `Parser.parse` and returns `ParsedDocument(source_type="image")`.
- Image MIME/signature maps accept PNG and JPEG; invalid or undecodable payloads use `IMAGE_MALFORMED` or `INVALID_FILE_SIGNATURE`.
- Optional dependency group exposes Pillow, `pypdfium2`, PaddleOCR and a compatible PaddlePaddle backend without adding them to the default install.

- [ ] **Step 1: Write failing settings, parser, and validation tests**

```python
def test_parser_settings_default_to_cpu_and_bound_ocr_limits():
    parsed = ParserSettings()
    assert parsed.ocr_device == "cpu"
    assert parsed.ocr_enabled is False
    with pytest.raises(ValidationError):
        ParserSettings(ocr_min_confidence=2)

def test_image_parser_decodes_png_and_emits_ocr_metadata():
    payload = make_png_bytes("hello")
    parsed = ImageParser(FakeOcrEngine((OcrLine("hello", 0.98, None),))).parse(
        io.BytesIO(payload), ParseContext("doc-1", "scan.png")
    )
    assert parsed.blocks[0].text == "hello"
    assert parsed.blocks[0].metadata["ocr"] is True
    assert parsed.metadata["ocr_page_count"] == 1

def test_upload_validation_rejects_png_with_wrong_signature():
    upload = upload_file("scan.png", b"not-an-image", "image/png")
    with pytest.raises(UploadValidationError) as error:
        await prepare_upload(upload, UploadSettings())
    assert error.value.code == "INVALID_FILE_SIGNATURE"
```

- [ ] **Step 2: Run the focused tests to verify they fail**

Run: `.venv/bin/pytest -q tests/unit/config/test_settings.py tests/unit/parsing/test_image_parser.py tests/unit/documents/test_upload_validation.py`

Expected: FAIL because image extensions, OCR settings, `ImageParser`, and image signatures are absent.

- [ ] **Step 3: Implement the minimal configuration and image path**

Add image extensions and MIME/signatures, decode with Pillow under width/height/pixel limits, call the injected engine, clean lines into paragraph blocks, and produce page/box/confidence/OCR metadata. Add only the optional dependency group and environment examples required by the spec.

- [ ] **Step 4: Run focused tests to verify they pass**

Run: `.venv/bin/pytest -q tests/unit/config/test_settings.py tests/unit/parsing/test_image_parser.py tests/unit/documents/test_upload_validation.py`

Expected: PASS.

- [ ] **Step 5: Run existing text and upload regression tests**

Run: `.venv/bin/pytest -q tests/unit/parsing/test_text_markdown.py tests/unit/documents/test_upload_validation.py`

Expected: PASS with existing text behavior unchanged.

### Task 3: PDF page rendering and mixed text/OCR parsing

**Files:**
- Create: `rag_modules/parsing/pdf_rendering.py`
- Modify: `rag_modules/parsing/pdf_parser.py`
- Modify: `rag_modules/config/settings.py` only for renderer DPI/limits if needed by the bounded fields
- Test: `tests/unit/parsing/test_pdf_ocr.py`

**Interfaces:**
- `PdfPageRenderer.render(page) -> object` is a small injectable protocol; the default implementation uses an optional renderer dependency and never imports it at module import time.
- `PdfParser(ocr_engine=None, page_renderer=None)` keeps the existing constructor compatibility and parses text pages without calling either OCR engine or renderer.
- Empty pages call OCR once, preserve `page` metadata, and emit `PDF_EMPTY_PAGE` only when OCR yields no usable block; mixed documents retain both text and OCR blocks.

- [ ] **Step 1: Write failing PDF OCR tests**

```python
def test_text_pdf_page_does_not_call_ocr():
    engine = RecordingOcrEngine()
    parsed = PdfParser(engine, FakePageRenderer()).parse(pdf_bytes("text page"), ParseContext("d", "a.pdf"))
    assert [block.text for block in parsed.blocks] == ["text page"]
    assert engine.calls == []

def test_empty_pdf_page_is_rendered_and_ocr_text_is_kept():
    engine = RecordingOcrEngine((OcrLine("扫描页", 0.99, None),))
    parsed = PdfParser(engine, FakePageRenderer()).parse(image_only_pdf_bytes(), ParseContext("d", "scan.pdf"))
    assert [block.text for block in parsed.blocks] == ["扫描页"]
    assert parsed.metadata["ocr_page_count"] == 1
    assert parsed.blocks[0].metadata["ocr"] is True

def test_mixed_pdf_only_ocr_calls_empty_page():
    engine = RecordingOcrEngine((OcrLine("扫描内容", 0.99, None),))
    parsed = PdfParser(engine, FakePageRenderer()).parse(mixed_pdf_bytes(), ParseContext("d", "mixed.pdf"))
    assert [block.text for block in parsed.blocks] == ["文本层", "扫描内容"]
    assert engine.calls == [1]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest -q tests/unit/parsing/test_pdf_ocr.py`

Expected: FAIL because the renderer injection and OCR fallback do not exist.

- [ ] **Step 3: Implement renderer protocol and mixed parser**

Keep pypdf extraction and existing malformed/encrypted/page-limit behavior. For each page, normalize text first; only if no paragraph exists, render and recognize. Convert cleaned OCR blocks to existing `ParsedBlock`, collect bounded warnings, count OCR pages, and map renderer/engine failures to stable errors. If all text and OCR are empty, retain `PDF_NO_EXTRACTABLE_TEXT`.

- [ ] **Step 4: Run PDF tests and existing PDF regression tests**

Run: `.venv/bin/pytest -q tests/unit/parsing/test_pdf_ocr.py tests/unit/parsing/test_pdf_docx.py`

Expected: PASS, including encrypted, malformed, text-and-empty-page, and image-only fixture behavior.

- [ ] **Step 5: Run `git diff --check`**

Expected: no whitespace errors.

### Task 4: Registry, factory, indexing, preview, and warning integration

**Files:**
- Modify: `rag_modules/parsing/factory.py`
- Modify: `rag_modules/parsing/registry.py` only if parser construction/config injection requires it
- Modify: `rag_modules/indexing/engine.py`
- Modify: `rag_modules/services/preview_service.py` only where source validation or warning handling needs synchronization
- Test: `tests/unit/parsing/test_registry_factory.py`
- Test: `tests/unit/indexing/test_document_engine.py`
- Test: `tests/unit/services/test_preview_service.py`

**Interfaces:**
- Factory builds one configured registry per process and registers `.png`, `.jpg`, `.jpeg` with `ImageParser`, plus the configured `PdfParser`.
- `_SUPPORTED_EXTENSIONS` and `_SAFE_WARNING_CODES` include image formats and OCR warning codes.
- Preview source validation accepts the same extensions as upload validation, and `PreviewDocument.source_metadata` plus `PreviewChunk.source_metadata` preserve OCR flags.

- [ ] **Step 1: Write failing integration tests**

```python
def test_factory_registers_image_extensions(monkeypatch):
    registry = get_parser_registry()
    assert {".png", ".jpg", ".jpeg"}.issubset(registry.registered_extensions)

def test_indexing_engine_keeps_safe_ocr_warnings():
    result = DocumentIndexingEngine(...). _safe_warnings(
        (ParserWarning("OCR_REPEATED_HEADER_FOOTER_REMOVED", "safe", {}),), "parse"
    )
    assert result[0].code == "OCR_REPEATED_HEADER_FOOTER_REMOVED"

def test_preview_accepts_image_source_and_returns_ocr_metadata():
    response = await preview_service.preview("dataset-1", ["doc-1"], image_preview_request())
    assert response.documents[0].source_metadata["ocr"] is True
    assert response.chunks[0].source_metadata["ocr"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest -q tests/unit/parsing/test_registry_factory.py tests/unit/indexing/test_document_engine.py tests/unit/services/test_preview_service.py`

Expected: FAIL because image extensions are not registered/allowed and OCR warnings are filtered.

- [ ] **Step 3: Implement synchronized integration**

Inject one configured engine/renderer into factory-created parsers, extend allowlists, preserve existing source metadata validation, and add only stable OCR warnings to safe serialization. Do not add a new API route.

- [ ] **Step 4: Run focused tests and existing registry/preview/indexing tests**

Run: `.venv/bin/pytest -q tests/unit/parsing/test_registry_factory.py tests/unit/parsing/test_registry.py tests/unit/indexing/test_document_engine.py tests/unit/services/test_preview_service.py`

Expected: PASS.

### Task 5: API and frontend source information with existing post-segmentation editing

**Files:**
- Modify: `rag_modules/api/dto/indexing_preview.py` only if OCR metadata needs an explicit bounded field
- Modify: `frontend/src/components/IndexingPanel.jsx`
- Modify: `frontend/src/components/SegmentEditor.jsx` only to show safe page/OCR source hints while retaining existing edit/save calls
- Modify: `frontend/src/styles.css` if a small metadata style is required
- Test: `tests/api/test_indexing_preview_api.py`
- Test: `frontend/src` existing test setup or a focused component test if the repository has a configured frontend runner

**Interfaces:**
- Existing `/indexing/preview` request and response paths remain unchanged.
- OCR metadata is rendered as a source hint; content/question/answer/keywords remain editable only through the existing segment revision API.
- Error messages map stable OCR codes to safe Chinese text and never display raw PaddleOCR exceptions.

- [ ] **Step 1: Write a failing API serialization test**

```python
async def test_preview_serializes_ocr_source_metadata_and_warning(client):
    response = client.post(PREVIEW_PATH, json=preview_payload_for_image())
    assert response.status_code == 200
    assert response.json()["documents"][0]["source_metadata"]["ocr"] is True
    assert response.json()["warnings"][0]["code"] == "OCR_REPEATED_HEADER_FOOTER_REMOVED"
```

- [ ] **Step 2: Run the API test to verify it fails**

Run: `.venv/bin/pytest -q tests/api/test_indexing_preview_api.py -k ocr`

Expected: FAIL because the fixture/serialization path does not yet expose OCR metadata safely.

- [ ] **Step 3: Implement the minimal UI/API integration**

Keep DTOs permissive only for bounded safe metadata already produced by parsers, add a small “OCR page source” hint beside each preview/segment, and reuse the current PATCH revision workflow. Do not add source-text editing before segmentation.

- [ ] **Step 4: Run API and frontend checks**

Run: `.venv/bin/pytest -q tests/api/test_indexing_preview_api.py tests/api/test_document_api.py`

Run: `npm test -- --runInBand` from `frontend/` when a frontend test script exists; otherwise run the repository’s configured frontend lint/build command and record its actual result.

Expected: existing API behavior passes and the frontend build/lint has no new errors.

### Task 6: Documentation and operational setup

**Files:**
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `docs/pending-features.md`
- Modify: `.env.example`
- Test: `tests/unit/config/test_settings.py` for environment parsing examples where practical

**Interfaces:**
- Documentation states supported image formats, mixed PDF behavior, optional installation, CPU/GPU configuration, model cache, and matching API/Worker environments.
- Pending-features documentation removes OCR from incomplete features and leaves table/layout/formula/handwriting recognition as future work.

- [ ] **Step 1: Add a failing configuration documentation behavior test**

```python
def test_ocr_environment_values_are_loaded(monkeypatch):
    monkeypatch.setenv("PARSER__OCR_ENABLED", "true")
    monkeypatch.setenv("PARSER__OCR_DEVICE", "cpu")
    loaded = Settings()
    assert loaded.parser.ocr_enabled is True
    assert loaded.parser.ocr_device == "cpu"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest -q tests/unit/config/test_settings.py -k ocr`

Expected: FAIL until the fields are wired to `ParserSettings`.

- [ ] **Step 3: Update docs and examples**

Document the optional installation command, CPU default, GPU backend selection, model cache path, and the requirement that API and Worker install the same OCR extras. Explicitly state that post-segmentation manual cleaning creates a revision and leaves the original upload unchanged.

- [ ] **Step 4: Run documentation/config checks**

Run: `.venv/bin/pytest -q tests/unit/config/test_settings.py -k ocr`

Run: `git diff --check`

Expected: PASS and no whitespace errors.

### Task 7: Full regression and acceptance verification

**Files:**
- Modify only files required by failing regression results from Tasks 1–6.
- Test: all existing `tests/` plus new OCR tests.

**Interfaces:**
- No real PaddleOCR model or GPU is required for the test suite.
- Existing text, Markdown, Office, tabular, PDF, preview, indexing, revision, and upload flows remain green.

- [ ] **Step 1: Run the complete test suite**

Run: `.venv/bin/pytest -q`

Expected: all collected tests pass; integration tests that require explicit external services may remain skipped according to existing markers.

- [ ] **Step 2: Run frontend validation**

Run the frontend package’s configured test/lint/build commands and capture the actual output.

- [ ] **Step 3: Perform an acceptance smoke test with fake OCR**

Use a generated PNG and fake engine to verify upload validation, parser registry dispatch, preview metadata, segmentation, and existing segment revision payload shape. Verify the original source bytes are unchanged.

- [ ] **Step 4: Run final hygiene checks**

Run: `git diff --check` and `git status --short`

Expected: no whitespace errors; list all changed files. Do not claim a Git commit because `.git/index.lock` is not writable in this environment.
