# PaddleOCR 文档解析与清洗设计

## 1. 目标

为知识库增加 PaddleOCR 文档解析能力，支持扫描版 PDF 和常见图片文件。系统保留现有 PDF 文本层解析路径，仅对没有可提取文本的 PDF 页面使用 OCR；图片文件直接使用 OCR。OCR 结果进入现有的统一解析、分段、预览和索引流水线。

用户确认的人工清洗发生在分段之后。OCR 结果先经过标准自动清洗并显示在分段预览中；首次索引完成后，用户通过现有分段编辑器修改块内容，保存为不可变文档修订并触发安全重建。

## 2. 范围与非目标

### 2.1 本期范围

- 新增 `.png`、`.jpg`、`.jpeg` 图片上传和解析。
- PDF 按页尝试提取文本层；空文本页转为图像并调用 PaddleOCR。
- 直接上传的图片调用 PaddleOCR。
- 以注入式 OCR 引擎隔离第三方 SDK，默认 CPU 运行，允许通过配置选择 GPU。
- OCR 行级结果做标准自动清洗：控制字符和多余空白规范化、相邻行合并、低置信度行过滤、跨页重复页眉/页脚过滤。
- 保留页码、边界框、平均置信度和 OCR 标识等安全元数据。
- OCR 解析结果继续通过 `ParserRegistry`、`Segmenter`、预览接口和索引引擎。
- OCR 识别失败、依赖缺失、模型初始化失败和无有效结果使用稳定的解析错误或 warning 返回。
- 复用现有“分段后手动编辑” API 和前端 `SegmentEditor`，不修改原始上传对象。

### 2.2 非目标

- 不强制对 DOCX、XLS/XLSX、TXT、Markdown 或已有文本层 PDF 进行 OCR。
- 不在首次索引前增加新的源文本编辑器。
- 不保存 OCR 覆盖后的文件，也不覆盖 MinIO 中的原始文件。
- 不在测试中下载或加载真实 PaddleOCR 模型。
- 不实现表格识别、版面结构重建、公式识别、手写识别或图像理解。
- 不把 PaddleOCR 绑定到单一部署硬件；部署环境负责安装匹配的 PaddlePaddle CPU/GPU 后端。

## 3. 用户流程

### 3.1 上传与解析

1. 上传校验接受 `.png`、`.jpg`、`.jpeg`，校验 MIME 类型、文件大小和图像签名。
2. 原始文件按现有规则写入 MinIO，源对象不可变。
3. 预览或正式索引调用统一 `ParserRegistry`。
4. 图片解析器将二进制图像交给 OCR 引擎；PDF 解析器逐页提取文本层，只有空文本页进入 OCR。
5. 解析器输出 `ParsedDocument`，OCR 行转换为 `ParsedBlock("paragraph", ...)`，每个 block 保存页码和 OCR 元数据。
6. 现有 `Segmenter` 生成分段，预览接口返回分段和 warning。

### 3.2 分段后手动清洗

1. 用户在索引配置中查看 OCR 清洗后的分段预览。
2. 首次索引完成后，用户打开现有分段编辑器修改内容、问题、答案和关键词。
3. 保存时复用 `DocumentRevisionService`，把用户编辑的分段快照写入不可变 `document_revisions`，原始 OCR 文件保持不变。
4. 修订任务由现有索引 Worker 使用 `revision_segments` 直接重建，成功后按现有 revision 激活规则切换检索可见版本。

## 4. 架构

### 4.1 OCR 边界

新增 `OcrEngine` 协议和 `PaddleOcrEngine` 适配器。协议只暴露稳定的内部结果结构，不让 PaddleOCR 的对象、异常或版本差异穿过解析层：

```python
class OcrEngine(Protocol):
    def recognize(self, image: "ImageInput") -> tuple[OcrLine, ...]: ...
```

`OcrLine` 至少包含 `text`、`confidence` 和可选 `box`。解析器接收 OCR 引擎实例，测试使用假引擎注入。真实适配器延迟导入 PaddleOCR，未安装可选依赖时抛出稳定的 `OCR_DEPENDENCY_MISSING`，避免普通文本文档在导入模块时失败。

### 4.2 图片输入

新增图片解析器负责安全读取图片并调用 OCR。图像尺寸、像素总量、页数（单图为 1）和 OCR 行数受配置限制。图像解码失败返回 `IMAGE_MALFORMED`；超过限制返回稳定的资源限制错误。实现可使用 Pillow 作为轻量图像解码边界，PaddleOCR 适配器只接收已验证的图像输入。

### 4.3 PDF 混合解析

`PdfParser` 继续使用 `pypdf` 读取文本层。每页：

- 提取到有效文本：沿用现有断词、换行和页码逻辑，不调用 OCR。
- 没有有效文本：将该页渲染为图像并调用 OCR；渲染失败或 OCR 依赖缺失时返回稳定错误，或在文档仍有其他有效内容时产生带页码的 warning。

若整份 PDF 没有文本层且 OCR 产生有效 block，解析成功，source metadata 标记 `ocr: true`；若文本层和 OCR 都没有有效 block，返回 `PDF_NO_EXTRACTABLE_TEXT`。现有加密、损坏和页数限制语义保持不变。

### 4.4 标准清洗

清洗在 OCR 行转成 `ParsedBlock` 前完成，规则固定且可测试：

- 统一 CRLF/CR 为 LF，移除 NUL 和有害控制字符。
- 将连续空白折叠为单个空格；保留中文和英文词间必要的单个空格。
- 删除空行、纯标点噪声和低于配置置信度的 OCR 行。
- 将同一页中垂直相邻、且没有明显段落间距的行合并为一个段落。
- 统计多个页面中相同且坐标位于页面顶部/底部 10% 边缘内的短文本；至少两页重复后，只从符合边缘条件的页面中删除，并记录 `OCR_REPEATED_HEADER_FOOTER_REMOVED` warning。缺少页面尺寸或文本坐标时保留内容，避免把稀疏页面的正文误判为页眉/页脚。
- 不猜测或改写低置信度文字；被过滤的内容只计数，不把原始识别文本写入正文。

清洗阈值放入 `ParserSettings`，提供保守默认值并设置最大范围，避免通过超大 OCR 输入绕过资源限制。置信度、box 和清洗动作统计只保存为 block/document metadata，不进入正文。

### 4.5 ParserRegistry 和上传格式

`SUPPORTED_UPLOAD_EXTENSIONS` 增加三种图片扩展名，`factory.py` 注册图片解析器。上传校验增加图片 MIME 集合和 magic/signature 校验。索引引擎的允许扩展名集合与预览源校验必须同步更新，避免上传成功后预览或 Worker 拒绝图片。

### 4.6 配置与依赖

PaddleOCR 和 PaddlePaddle 放入可选依赖组，避免基础安装自动拉取平台相关的大型包。配置至少包括：

- OCR 是否启用。
- 设备：`cpu` 或 `gpu`，默认 `cpu`。
- 语言/模型配置。
- 最低行置信度。
- 最大图片宽高和最大像素数。
- 单文档最大 OCR 行数。
- OCR 初始化和调用超时（若底层支持）。

真实 Worker 和 API 进程都必须安装同一 OCR 可选依赖并读取同一配置；未启用 OCR 时，文本层 PDF、文本文件和 Office 文件仍可正常工作。扫描 PDF 或图片在 OCR 未启用时返回明确的 `OCR_NOT_ENABLED`，不伪装成空文档。

## 5. 错误与 warning 契约

稳定错误码包括：

- `OCR_NOT_ENABLED`：需要 OCR 但配置关闭。
- `OCR_DEPENDENCY_MISSING`：配置启用但 PaddleOCR/PaddlePaddle 未安装。
- `OCR_INITIALIZATION_FAILED`：模型或设备初始化失败。
- `OCR_FAILED`：识别过程失败。
- `IMAGE_MALFORMED`：图像无法安全解码。
- `IMAGE_DIMENSION_LIMIT_EXCEEDED`：图像尺寸或像素预算超限。
- `OCR_NO_EXTRACTABLE_TEXT`：图片或扫描页没有达到最低质量的识别结果。

非致命的空扫描页继续使用 bounded warning collector。OCR 警告必须通过现有预览 warning 和索引 warning 白名单，不能暴露第三方异常文本或模型路径。

## 6. API 与前端

不新增独立 OCR API。现有上传、索引预览和索引任务接口保持路径与请求结构不变：

- 上传接口接受新增图片扩展名。
- `/indexing/preview` 返回的 `PreviewDocument.source_metadata` 标出 OCR 页数/是否使用 OCR，`PreviewChunk.source_metadata` 标出页码和 OCR 来源。
- 现有 `SegmentEditor` 继续提供分段后手动编辑；在 OCR 分段上显示与普通分段相同的编辑控件，并在有 OCR metadata 时显示页面来源提示。
- 错误消息使用安全、稳定的中文前端提示；不显示 PaddleOCR 原始异常。

## 7. 测试策略

### 7.1 单元测试

- 标准清洗：空白、控制字符、低置信度、相邻行合并和重复页眉/页脚。
- `PaddleOcrEngine` 的延迟依赖加载和第三方结果适配，使用 monkeypatch/fake module，不安装模型。
- 图片解析器：有效图片、坏图片、无有效 OCR 文本、尺寸限制和 metadata。
- PDF：文本页不调用 OCR、空文本页调用 OCR、混合 PDF、纯扫描 PDF 和 OCR 失败。
- ParserRegistry/上传校验/索引引擎允许扩展名同步。

### 7.2 API 与前端回归

- 上传图片后可在预览接口中得到 OCR 分段。
- OCR warning 和稳定错误码可安全序列化。
- 现有分段编辑 API 保存 OCR 分段修订并触发重建，原始对象不被覆盖。
- 现有 TXT、Markdown、Office、文本 PDF 测试全部保持通过。

测试不依赖外部 MinIO、PaddleOCR 模型、GPU、Milvus 或 RabbitMQ；集成测试沿用项目已有的显式环境约束。

## 8. 文档与运维

- 更新 `README.md`、`docs/architecture.md`、`docs/pending-features.md`，说明新增格式、可选依赖、CPU/GPU 配置、模型缓存和 Worker 安装要求。
- 提供 `.env.example` 中的 OCR 配置示例，不提交模型文件或密钥。
- 记录当前已支持的是 OCR 文本提取和标准清洗，表格/版面/公式识别仍属于后续能力。

## 9. 验收标准

1. 上传 `.png/.jpg/.jpeg` 能通过现有上传 API；非法图片被稳定拒绝。
2. 有文本层的 PDF 不调用 OCR；扫描 PDF 页面可以经 OCR 生成分段。
3. 混合 PDF 的文本页和扫描页都能按页保留来源定位。
4. 自动清洗结果可从预览中看到，低置信度噪声和重复页眉/页脚不会进入正文。
5. 首次索引后，现有分段编辑器可修改 OCR 分段并触发 revision 重建；原始文件仍可重新解析。
6. OCR 未安装或未启用时，普通文档流程不受影响，扫描文档返回明确错误。
7. 相关单元、API 和前端回归测试通过。
