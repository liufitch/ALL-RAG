# PaddleOCR 文档解析与清洗实施计划

> **面向代理工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 按任务逐步实施本计划。各步骤使用 checkbox（`- [ ]`）语法跟踪进度。

**目标：** 在现有统一解析、预览、索引和分段修订流程中增加 PaddleOCR 图片与扫描版 PDF 解析，并提供标准自动清洗和分段后的手动编辑支持。

**架构：** 以 `OcrEngine` 协议隔离 PaddleOCR，真实适配器延迟导入可选依赖，测试通过 fake engine 注入。图片解析器直接处理已验证的图像；PDF 逐页优先读取文本层，仅把没有文本的页面交给可注入的页面渲染器和 OCR。OCR 输出先经过固定清洗，再生成现有 `ParsedDocument`，沿用 `ParserRegistry`、`PreviewService`、索引引擎和 `DocumentRevisionService`。

**技术栈：** Python 3.11、Pydantic Settings、pypdf、Pillow、可选 PaddleOCR/PaddlePaddle、现有 FastAPI/Celery/React 流程、pytest。

**规格：** `docs/superpowers/specs/2026-10-24-paddleocr-document-processing-design.md`

## 最终实现与验证记录（2026-10-24）

功能实现已落地：图片和空文本 PDF 页面 OCR、标准清洗、来源元数据、统一预览与索引，以及复用现有分段编辑器保存 revision。下方保留原始实施步骤作为设计与测试参考。

- 后端完整回归：`PYTHONPATH=. .venv/bin/pytest -q`，**664 passed, 3 skipped**。
- 前端：`npm test`，**17 passed**；`npm run build` 成功。
- `python3 -m compileall -q rag_modules` 与 `git diff --check` 通过。
- 独立代码审查后补充并修复了 BGR 输入、NumPy 边界框、识别结果与边界框对齐、页边缘重复文本过滤、整份文档 OCR 行数预算及共享模型实例的回归用例；复审未发现尚未解决的重要问题。
- 自动化测试使用模拟 OCR/解码/渲染边界，并使用真实 NumPy 验证数组适配。后续在 Docker Desktop `linux/amd64` CPU 容器中安装并验证 PaddleOCR 3.7.0、PaddlePaddle 3.2.2、Pillow 12.3.0、pypdfium2 5.14.0；PP-OCRv5 成功识别合成中英文 PNG 和扫描 PDF，清洗正文、页码、置信度及文本框元数据符合预期。GPU 和生产文档识别效果尚未验证。
- 改动保留在工作区，未提交 Git。

## 全局约束

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

### 任务 1：OCR 结果契约与标准清洗

**文件：**
- 创建: `rag_modules/parsing/ocr.py`
- 创建: `rag_modules/parsing/ocr_cleaning.py`
- 修改: `rag_modules/parsing/models.py` only if a shared metadata type is needed; retain existing public dataclasses
- 测试: `tests/unit/parsing/test_ocr_cleaning.py`
- 测试: `tests/unit/parsing/test_ocr_engine.py`

**接口：**
- 生成 `OcrLine(text: str, confidence: float, box: tuple[tuple[float, float], ...] | None)`、`OcrEngine.recognize(image: object) -> tuple[OcrLine, ...]` 和 `clean_ocr_lines(lines, *, page_number, settings, repeated_lines) -> CleanedOcrPage`。
- `CleanedOcrPage` 向图片/PDF parser 提供清洗后的段落字符串、保留的行元数据、过滤行数和重复页眉/页脚移除标记。
- 未安装 PaddleOCR 时仍可导入 `PaddleOcrEngine`；构造或识别时只抛出 spec 定义的稳定 `DocumentParseError` 错误码。

- [ ] **步骤 1：编写失败的清洗测试**

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

- [ ] **步骤 2：运行测试以确认失败**

运行：`.venv/bin/pytest -q tests/unit/parsing/test_ocr_cleaning.py`

预期：失败，因为 OCR 结果和清洗器接口尚不存在。

- [ ] **步骤 3：编写失败的引擎适配器测试**

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

- [ ] **步骤 4：运行引擎测试以确认失败**

运行：`.venv/bin/pytest -q tests/unit/parsing/test_ocr_engine.py`

预期：失败，因为 `PaddleOcrEngine` 尚未实现。

- [ ] **步骤 5：实现最小清洗器和延迟加载适配器**

实现 `OcrLine`、协议、稳定异常映射、常见 PaddleOCR `ocr()`/`predict()` 响应结果形状规范化和确定性清洗。将 PaddleOCR 导入放在引擎初始化内部，根据 `ParserSettings` 限制置信度和行数行为，并且只返回安全 metadata。

- [ ] **步骤 6：运行两个测试文件**

运行：`.venv/bin/pytest -q tests/unit/parsing/test_ocr_cleaning.py tests/unit/parsing/test_ocr_engine.py`

预期：通过。

- [ ] **步骤 7：运行 `git diff --check` 并将任务记录为完成**

运行：`git diff --check`

预期：无空白错误。由于当前环境无法创建 `.git/index.lock`，将改动保留在工作树中。

### 任务 2：配置、图片 parser 和上传校验

**文件：**
- 创建: `rag_modules/parsing/image_parser.py`
- 修改: `rag_modules/config/settings.py`
- 修改: `rag_modules/upload_formats.py`
- 修改: `rag_modules/documents/validation.py`
- 修改: `pyproject.toml`
- 修改: `.env.example`
- 测试: `tests/unit/parsing/test_image_parser.py`
- 测试: `tests/unit/documents/test_upload_validation.py`
- 测试: `tests/unit/config/test_settings.py`

**接口：**
- `ParserSettings` 增加有界 OCR 字段：`ocr_enabled`、`ocr_device`、`ocr_language`、`ocr_model_dir`、`ocr_min_confidence`、`ocr_max_image_width`、`ocr_max_image_height`、`ocr_max_image_pixels` 和 `ocr_max_lines_per_document`。
- `ImageParser(engine: OcrEngine | None = None)` 实现 `Parser.parse`，并返回 `ParsedDocument(source_type="image")`。
- 图片 MIME/签名映射接受 PNG 和 JPEG；无效或无法解码的负载使用 `IMAGE_MALFORMED` 或 `INVALID_FILE_SIGNATURE`。
- 可选依赖组提供 Pillow、`pypdfium2`、PaddleOCR 和兼容的 PaddlePaddle backend，但不加入默认安装。

- [ ] **步骤 1：编写失败的设置、parser 和校验测试**

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

- [ ] **步骤 2：运行针对性测试以确认失败**

运行：`.venv/bin/pytest -q tests/unit/config/test_settings.py tests/unit/parsing/test_image_parser.py tests/unit/documents/test_upload_validation.py`

预期：失败，因为图片扩展名、OCR 设置、`ImageParser` 和图片签名尚不存在。

- [ ] **步骤 3：实现最小配置和图片处理路径**

添加图片扩展名和 MIME/签名，在宽度/高度/像素限制内使用 Pillow 解码，调用注入引擎，将行清洗为段落块，并生成页码/边界框/置信度/OCR metadata。只添加 spec 要求的可选依赖组和环境配置示例。

- [ ] **步骤 4：运行针对性测试以确认通过**

运行：`.venv/bin/pytest -q tests/unit/config/test_settings.py tests/unit/parsing/test_image_parser.py tests/unit/documents/test_upload_validation.py`

预期：通过。

- [ ] **步骤 5：运行现有文本和上传回归测试**

运行：`.venv/bin/pytest -q tests/unit/parsing/test_text_markdown.py tests/unit/documents/test_upload_validation.py`

预期：通过，且现有文本处理行为保持不变。

### 任务 3：PDF 页面渲染与文本/OCR 混合解析

**文件：**
- 创建: `rag_modules/parsing/pdf_rendering.py`
- 修改: `rag_modules/parsing/pdf_parser.py`
- 修改: `rag_modules/config/settings.py` only for renderer DPI/limits if needed by the bounded fields
- 测试: `tests/unit/parsing/test_pdf_ocr.py`

**接口：**
- `PdfPageRenderer.render(page) -> object` 是一个轻量且可注入的协议；默认实现使用可选的 renderer 依赖，并且绝不在模块导入时加载该依赖。
- `PdfParser(ocr_engine=None, page_renderer=None)` 保持现有构造函数兼容性；解析文本页时不会调用 OCR engine 或 renderer。
- 空白页面调用一次 OCR，保留 `page` 元数据；只有 OCR 未产生可用 block 时才发出 `PDF_EMPTY_PAGE`。混合文档同时保留文本层和 OCR 生成的 block。

- [ ] **步骤 1：编写失败的 PDF OCR 测试**

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

- [ ] **步骤 2：运行测试以确认失败**

运行：`.venv/bin/pytest -q tests/unit/parsing/test_pdf_ocr.py`

预期：失败，因为渲染器注入和 OCR 回退尚不存在。

- [ ] **步骤 3：实现渲染器协议和混合解析器**

保留 pypdf 提取和现有格式错误/加密/页数限制行为。逐页先规范化文本；只有不存在段落时才渲染并识别。将清洗后的 OCR 块转换为现有 `ParsedBlock`，收集有界 warning，统计 OCR 页数，并将渲染器/引擎失败映射为稳定错误。如果文本和 OCR 结果均为空，则继续使用 `PDF_NO_EXTRACTABLE_TEXT`。

- [ ] **步骤 4：运行 PDF 测试和现有 PDF 回归测试**

运行：`.venv/bin/pytest -q tests/unit/parsing/test_pdf_ocr.py tests/unit/parsing/test_pdf_docx.py`

预期：通过，覆盖加密、格式错误、文本页与空白页混合以及纯图像 fixture 行为。

- [ ] **步骤 5：运行 `git diff --check`**

预期：无空白错误。

### 任务 4：Registry、factory、索引、预览和 warning 集成

**文件：**
- 修改: `rag_modules/parsing/factory.py`
- 修改: `rag_modules/parsing/registry.py` only if parser construction/config injection requires it
- 修改: `rag_modules/indexing/engine.py`
- 修改: `rag_modules/services/preview_service.py` only where source validation or warning handling needs synchronization
- 测试: `tests/unit/parsing/test_registry_factory.py`
- 测试: `tests/unit/indexing/test_document_engine.py`
- 测试: `tests/unit/services/test_preview_service.py`

**接口：**
- Factory 为每个进程构建一个已配置的 registry，并将 `.png`、`.jpg`、`.jpeg` 注册到 `ImageParser`，同时注册已配置的 `PdfParser`。
- `_SUPPORTED_EXTENSIONS` 和 `_SAFE_WARNING_CODES` 包含图像格式和 OCR warning code。
- 预览源校验接受与上传校验相同的扩展名；`PreviewDocument.source_metadata` 和 `PreviewChunk.source_metadata` 均保留 OCR 标记。

- [ ] **步骤 1：编写失败的集成测试**

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

- [ ] **步骤 2：运行测试以确认失败**

运行：`.venv/bin/pytest -q tests/unit/parsing/test_registry_factory.py tests/unit/indexing/test_document_engine.py tests/unit/services/test_preview_service.py`

预期：失败，因为图片扩展名尚未注册/允许，且 OCR warning 会被过滤。

- [ ] **步骤 3：实现同步集成**

向 factory 创建的 parser 注入同一个已配置引擎/渲染器，扩展允许列表，保留现有源 metadata 校验，并仅将稳定的 OCR warning 加入安全序列化。不要添加新的 API 路由。

- [ ] **步骤 4：运行针对性测试和现有 registry/预览/索引测试**

运行：`.venv/bin/pytest -q tests/unit/parsing/test_registry_factory.py tests/unit/parsing/test_registry.py tests/unit/indexing/test_document_engine.py tests/unit/services/test_preview_service.py`

预期：通过。

### 任务 5：API 与前端来源信息及现有分段后编辑流程

**文件：**
- 修改: `rag_modules/api/dto/indexing_preview.py` only if OCR metadata needs an explicit bounded field
- 修改: `frontend/src/components/IndexingPanel.jsx`
- 修改: `frontend/src/components/SegmentEditor.jsx` only to show safe page/OCR source hints while retaining existing edit/save calls
- 修改: `frontend/src/styles.css` if a small metadata style is required
- 测试: `tests/api/test_indexing_preview_api.py`
- 测试: `frontend/src` existing test setup or a focused component test if the repository has a configured frontend runner

**接口：**
- 现有 `/indexing/preview` 请求和响应路径保持不变。
- OCR 元数据显示为来源提示；content/question/answer/keywords 仍只能通过现有分段 revision API 编辑。
- 错误消息将稳定的 OCR code 映射为安全的中文文本，绝不展示 PaddleOCR 原始异常。

- [ ] **步骤 1：编写失败的 API 序列化测试**

```python
async def test_preview_serializes_ocr_source_metadata_and_warning(client):
    response = client.post(PREVIEW_PATH, json=preview_payload_for_image())
    assert response.status_code == 200
    assert response.json()["documents"][0]["source_metadata"]["ocr"] is True
    assert response.json()["warnings"][0]["code"] == "OCR_REPEATED_HEADER_FOOTER_REMOVED"
```

- [ ] **步骤 2：运行 API 测试以确认失败**

运行：`.venv/bin/pytest -q tests/api/test_indexing_preview_api.py -k ocr`

预期：失败，因为 fixture/序列化路径尚未安全暴露 OCR metadata。

- [ ] **步骤 3：实现最小 UI/API 集成**

DTO 仅对 parser 已生成的有界安全 metadata 保持宽容，在每个预览/分段旁添加简短的“OCR 页面来源”提示，并复用当前 PATCH 修订流程。不要增加分段前的源文本编辑。

- [ ] **步骤 4：运行 API 和前端检查**

运行：`.venv/bin/pytest -q tests/api/test_indexing_preview_api.py tests/api/test_document_api.py`

如果存在前端测试脚本，则在 `frontend/` 中运行 `npm test -- --runInBand`；否则运行仓库配置的前端 lint/build 命令，并记录实际结果。

预期：现有 API 行为测试通过，前端 build/lint 没有新增错误。

### 任务 6：文档和运维配置

**文件：**
- 修改: `README.md`
- 修改: `docs/architecture.md`
- 修改: `docs/pending-features.md`
- 修改: `.env.example`
- 测试: `tests/unit/config/test_settings.py` for environment parsing examples where practical

**接口：**
- 文档说明支持的图像格式、混合 PDF 行为、可选安装方式、CPU/GPU 配置、模型缓存，以及 API/Worker 环境一致性要求。
- 待办功能文档将 OCR 从未完成功能中移除，并将表格/版面/公式/手写识别保留为后续工作。

- [ ] **步骤 1：添加失败的配置文档行为测试**

```python
def test_ocr_environment_values_are_loaded(monkeypatch):
    monkeypatch.setenv("PARSER__OCR_ENABLED", "true")
    monkeypatch.setenv("PARSER__OCR_DEVICE", "cpu")
    loaded = Settings()
    assert loaded.parser.ocr_enabled is True
    assert loaded.parser.ocr_device == "cpu"
```

- [ ] **步骤 2：运行测试以确认失败**

运行：`.venv/bin/pytest -q tests/unit/config/test_settings.py -k ocr`

预期：字段接入 `ParserSettings` 前测试失败。

- [ ] **步骤 3：更新文档和示例**

记录可选安装命令、默认 CPU 模式、GPU backend 选择、模型缓存路径，以及 API 和 Worker 必须安装相同 OCR extras 的要求。明确说明：分段后的手动清洗会创建一个 revision，且不会修改原始上传文件。

- [ ] **步骤 4：运行文档/配置检查**

运行：`.venv/bin/pytest -q tests/unit/config/test_settings.py -k ocr`

运行：`git diff --check`

预期：通过且无空白错误。

### 任务 7：完整回归和验收验证

**文件：**
- 仅修改任务 1–6 的失败回归结果所必需的文件。
- 测试：所有现有 `tests/` 以及新增 OCR 测试。

**接口：**
- 测试套件不需要真实 PaddleOCR 模型或 GPU。
- 现有文本、Markdown、Office、表格、PDF、预览、索引、修订和上传流程测试均保持通过。

- [ ] **步骤 1：运行完整测试套件**

运行：`.venv/bin/pytest -q`

预期：所有收集到的测试通过；依赖明确外部服务的集成测试可按现有标记跳过。

- [ ] **步骤 2：运行前端验证**

运行前端包配置的 test/lint/build 命令并记录实际输出。

- [ ] **步骤 3：使用 fake OCR 执行验收冒烟测试**

使用生成的 PNG 和 fake engine 验证上传校验、parser registry 分派、预览 metadata、分段和现有分段修订负载形状。确认原始源字节未改变。

- [ ] **步骤 4：运行最终整洁性检查**

运行：`git diff --check` 和 `git status --short`

预期：无空白错误，并列出所有变更文件。由于当前环境无法写入 `.git/index.lock`，不要声称已创建 Git 提交。
