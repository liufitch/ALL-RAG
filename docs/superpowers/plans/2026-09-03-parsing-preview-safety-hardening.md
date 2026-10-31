# 解析与预览安全加固实施计划

> **面向代理工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 按任务逐步实施本计划。各步骤使用 checkbox（`- [ ]`）语法跟踪进度。

**目标：** 在 Embedding/Milvus 阶段开始前，解决五项遗留的 XLSX、warning、分段、分隔符保真度和 YAML 资源安全问题。

**架构：** 保持公开 parser、`Segmenter` 和预览接口不变。通过基于关系解析的 OOXML 预检、有界 warning collector、预计及实测分段工作预算、保留保真度的分隔符附加，以及对象构造前的 YAML 事件预检，加强内部安全边界。

**技术栈：** Python 3.11、FastAPI、Pydantic v2、AnyIO、PyYAML、lxml、OpenPyXL、pytest。

**规格：** `docs/superpowers/specs/2026-09-03-parsing-preview-safety-hardening-design.md`

## 全局约束

- 预览和未来的索引 Worker 必须继续使用同一 parser registry 和 `Segmenter` 实现。
- 请求超时只是纵深防御；每个被放弃的同步 parser 或 segmenter 操作都必须有有限且由输入规模推导的工作上限。
- XLSX 关系目标不得逃出 OOXML 包，不得使用外部目标，不得通过文件名大小写绕过预检；每个工作表部件通过预检前不得调用 OpenPyXL。
- 绝不计算公式。优先使用缓存值；缓存缺失时保留公式文本，并发出有界聚合 warning，warning 不包含公式内容。
- 公开的普通、父级和子级分段始终满足 `content.strip() != ""`；常规完整文档的父块拼接必须精确保留真实 `"\n\n"` 边界。
- YAML 锚点、别名、过深嵌套、过多事件、递归、重复键、非 JSON 值、非有限浮点数和无界标量都不得进入文档元数据。
- 资源限制和格式错误必须返回稳定且安全的错误码/消息，绝不包含源内容、关系目标、公式、后端异常文本、凭据或完整元数据。
- 每项行为变更都以提交 `c567afc` 为基准遵循 RED-GREEN TDD（其生产行为为已评审的 `8fb64f4` 实现加本整改设计文档）。
- 每项任务都要将遇到的症状、根因、失败方案、结论、修复提交、RED/GREEN 命令和剩余风险追加到 `docs/superpowers/retrospectives/2026-09-03-dataset-indexing-issues.md`；该受跟踪的回顾记录是长期记录，绝不能被忽略的 SDD 台账替代。
- 不添加 OCR、`.doc`、公式求值、新文件格式、新 API 路由、持久化、Embedding、Milvus、Celery、前端改动或无关重构。

---

## 文件结构

- 修改：`rag_modules/config/settings.py` — 合并范围、解析器 warning、预览 warning 和分段工作预算。
- 修改：`rag_modules/parsing/xlsx_parser.py` — 基于关系发现工作表、工作表 XML/合并范围预检、公式 warning 聚合、隔离的物理单元格适配器。
- 新建：`rag_modules/parsing/warnings.py` — 由 parser 和预览序列化共用的有界 warning collector。
- 修改：`rag_modules/parsing/__init__.py` — 仅当调用方需要公共导入时导出有界 warning collector。
- 修改：`rag_modules/services/preview_service.py` — 响应级 warning collector。
- 修改：`rag_modules/segmentation/segmenter.py` — 预计迭代次数、实测边界扫描、无复制边界查找、分隔符附加和保真度 warning。
- 修改：`rag_modules/parsing/markdown_parser.py` — YAML 事件预检和递归错误规范化。
- 修改：`tests/unit/config/test_settings.py` — 正数/默认预算解析和无效值。
- 修改：`tests/unit/parsing/test_tabular.py` — 对抗性关系、合并区域、稀疏单元格、公式聚合和 warning 上限。
- 新建：`tests/unit/parsing/test_warning_collector.py` — 精确验证有界 collector 行为。
- 修改：`tests/unit/services/test_preview_service.py` — 跨文档预览 warning 上限。
- 修改：`tests/unit/segmentation/test_segmenters.py` — 预计/实测工作限制和普通回退的精确重建。
- 修改：`tests/unit/parsing/test_text_markdown.py` — 深层块状/流式 YAML 和回归覆盖。
- 修改：`docs/superpowers/retrospectives/2026-09-03-dataset-indexing-issues.md` — 每项任务和评审发现的仅追加记录。

### 任务 1：基于关系解析的 XLSX 预检与合并范围限制

**接口：**

- 产出：按工作簿工作表顺序排列的不可变 `_WorksheetPreflight(title: str, part_name: str, physical_coordinates: frozenset[tuple[int, int]])` 记录。
- 产出：`_worksheet_parts(archive: ZipFile) -> tuple[tuple[str, str], ...]`，将每个工作表标题/关系解析为精确的规范包成员路径。仅接受 Transitional URI `http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet` 和 Strict URI `http://purl.oclc.org/ooxml/officeDocument/relationships/worksheet`；禁止后缀匹配。
- 产出：`_preflight_worksheet_xml(payload: bytes) -> tuple[_WorksheetPreflight, ...]`，仅校验已解析的工作表部件，且必须在运行 `load_workbook` 前完成；保留有界物理坐标集合。
- 产出：隔离的 `_physical_cells(worksheet, cached_worksheet, physical_coordinates) -> Iterator[tuple[int, int, Any, Any]]` 适配器，返回公式/缓存视图中的真实物理非占位单元格，不扩展声明维度、不创建缓存单元格，也不接受合并占位符。
- 使用：`settings.parser.max_spreadsheet_xml_nodes`、`max_physical_cells`、`max_row_coordinate`、`max_column_coordinate`、`max_merged_cell_area` 和 `max_total_merged_cell_area`。

**精确默认值和错误：**

- `ParserSettings.max_merged_cell_area = Field(default=100_000, ge=1)`.
- `ParserSettings.max_total_merged_cell_area = Field(default=1_000_000, ge=1)`；模型 validator 要求总面积大于或等于单一区域上限，且小于或等于 `max_physical_cells`。
- 关系/包损坏：`XLSX_MALFORMED`，消息 `The XLSX file is malformed or unreadable.`
- 单个合并区域超限：`TABLE_MERGED_CELL_LIMIT_EXCEEDED`，消息 `A merged spreadsheet range exceeds the configured area limit.`
- 合并区域总面积超限：`TABLE_TOTAL_MERGED_CELL_LIMIT_EXCEEDED`，消息 `The spreadsheet exceeds the configured merged-cell area limit.`

- [ ] **步骤 1：添加失败的设置和关系测试**

在 `tests/unit/config/test_settings.py` 中添加测试，覆盖精确默认值、正数校验、`total >= single` 和 `total <= max_physical_cells`。

扩展 `tests/unit/parsing/test_tabular.py` 中的 ZIP 测试辅助工具，使其可以重命名工作表成员并更新 `xl/_rels/workbook.xml.rels`。添加 RED 测试证明：

1. 将关系目标从 `worksheets/sheet1.xml` 改为大小写不同的 `worksheets/sheet1.XML` 后，仍会执行预检；若其中含有三个单元格且 `max_physical_cells=2`，则必须拒绝；
2. `TargetMode="External"`、绝对路径、`../` 路径穿越、目标缺失、工作表目标重复以及非工作表关系类型，均会触发 `XLSX_MALFORMED`；
3. 将 `rag_modules.parsing.xlsx_parser.load_workbook` monkeypatch 为失败函数，可证明被拒绝的包不会调用该函数。

- [ ] **步骤 2：运行关系 RED 测试**

运行：

```bash
.venv/bin/python -m pytest \
  tests/unit/config/test_settings.py \
  tests/unit/parsing/test_tabular.py \
  -k 'merged_cell or relationship or case_varied or external or traversal or missing_target or duplicate_target' -q
```

预期：因新设置尚不存在而失败；当前 glob 也会跳过大小写不同或关系无效的部件。

- [ ] **步骤 3：实现规范关系发现**

使用 lxml 的 `resolve_entities=False`、`no_network=True` 和 `huge_tree=False` 解析 `xl/workbook.xml` 与 `xl/_rels/workbook.xml.rels`。将工作簿工作表的 `r:id` 值匹配到唯一的内部工作表关系，并拒绝重复关系 ID。使用 `PurePosixPath` 以 `xl/workbook.xml` 为基准解析目标；拒绝空路径段、绝对路径、URL、查询/片段文本、百分号转义、反斜杠、`..`、外部模式、缺失成员、重复解析成员，以及除接口中列出的精确 Transitional 或 Strict 工作表 URI 以外的关系类型。

绝不通过小写化来打开 ZIP 成员。返回精确解析出的成员路径，并在创建任何一个 OpenPyXL 工作簿前流式预检所有部件。

- [ ] **步骤 4：添加失败的合并范围测试**

使用 `_xlsx_bytes` 修改 `sheet1.xml`，添加以下场景：

- 在较小单区上限下，一个真实 `A1` 单元格加 `<mergeCell ref="A1:Z100"/>`；
- 多个单独有效、但总面积超过总上限的范围；
- 格式错误、反向、超出工作表或缺失的合并引用。

使用快速失败函数 monkeypatch `load_workbook`，断言每次超限都发生在调用 OpenPyXL 之前。添加普通 `A1:B2` 合并工作簿回归测试，证明解析仍有界且不会将 `MergedCell` 占位符作为值输出。

- [ ] **步骤 5：运行合并范围 RED 测试**

运行：

```bash
.venv/bin/python -m pytest tests/unit/parsing/test_tabular.py \
  -k 'merge or merged or physical_cell_adapter' -q
```

预期：测试失败，因为当前预检忽略 `<mergeCell>` 面积，普通模式的 `_cells` 还可能包含已实例化的合并占位符。

- [ ] **步骤 6：实现合并预检和物理单元格适配器**

在工作表 `iterparse` 期间，使用 `openpyxl.utils.cell.range_boundaries`、整数面积运算、XLSX 坐标最大值和配置的单项/总面积上限校验每个 `<mergeCell ref>`，并用集合防止重复计算范围。保留现有节点/单元格/非空坐标限制。

将所有 `_cells` 访问移入 `_physical_cells`。如果预期的映射约定不可用、OpenPyXL 的工作表标题/顺序与预检结果不一致，或缓存/公式映射包含不可能出现的意外物理坐标，则以 `XLSX_MALFORMED` 失败。遍历排序后的预检坐标集合，并且只使用映射的 `.get()`。仅返回真实物理 `Cell` 值；跳过 `MergedCell` 占位符以及预检时未记录在工作表 XML 物理单元格集合中的坐标。不得调用 `worksheet.cell()` 来枚举源单元格或缓存单元格。

- [ ] **步骤 7： Run Task 1 tests and regressions**

运行:

```bash
.venv/bin/python -m pytest tests/unit/config/test_settings.py tests/unit/parsing/test_tabular.py -q
```

预期：关系、合并范围、声明维度、稀疏行、公式、隐藏/空工作表、表格限制和格式错误文件测试全部通过。

- [ ] **步骤 8： Append the Task 1 retrospective record**

将任务 1 的证据追加到 `docs/superpowers/retrospectives/2026-09-03-dataset-indexing-issues.md`，包括所有关系/合并范围症状、根因、被否决方案、最终设计、RED/GREEN 命令和结果、待创建提交以及剩余风险。不要重写早期条目。

- [ ] **步骤 9： Commit Task 1**

```bash
git add rag_modules/config/settings.py rag_modules/parsing/xlsx_parser.py \
  tests/unit/config/test_settings.py tests/unit/parsing/test_tabular.py \
  docs/superpowers/retrospectives/2026-09-03-dataset-indexing-issues.md
git commit -m "fix: bound xlsx worksheet materialization"
```

### 任务 2：有界解析器与预览 Warning

**接口：**

- 产出：泛型 `BoundedWarningCollector[T](limit: int, summary_factory: Callable[[int], T])`，提供 `add(warning: T)`、`extend(iterable: Iterable[T])` 和 `result() -> tuple[T, ...]`；`T` 满足暴露 `code: str` 与 `metadata: dict[str, Any]` 的小型 `WarningLike` 协议，因此同一个 collector 可同时支持 `ParserWarning` 和 `PreviewWarning`，且不丢失文档身份。
- 产出：每个工作表一个 `FORMULA_CACHE_UNAVAILABLE` warning，元数据为 `{"sheet": str, "count": int, "sample_cells": list[str]}`，样例最多五个。
- 产出：`WARNINGS_TRUNCATED`，消息为 `Additional warnings were omitted.`，元数据为 `{"omitted_count": int}`。
- 使用：`settings.parser.max_warnings_per_document=100`、`settings.parser.max_formula_warning_samples=5` 和 `settings.preview.max_warnings=100`。

**Collector 语义：**

- `limit` 包含截断摘要。
- 对于 `limit=1`，结果只有带总省略数量的 `WARNINGS_TRUNCATED`。
- 对于 `limit>1`，保留前 `limit - 1` 个 warning，并在最后一个位置汇总其余 warning。
- collector 绝不保留被省略的 warning 对象或其元数据。
- 添加已有的 `WARNINGS_TRUNCATED` warning 时，将其正整数 `omitted_count` 加入 collector 的省略总数，而不是嵌套摘要。调用方提供的 `summary_factory(omitted_count)` 根据需要创建 `ParserWarning` 或与文档无关的 `PreviewWarning`。

- [ ] **步骤 1：编写失败的 collector 测试**

创建 `tests/unit/parsing/test_warning_collector.py`，覆盖空输入、未达上限时的顺序、恰好达到上限、上限为 1、超限计数、连续 `extend`、已有摘要折叠、非正上限无效，以及每省略一项就释放其 warning 元数据的生成器。

- [ ] **步骤 2：运行 collector RED 测试**

运行：

```bash
.venv/bin/python -m pytest tests/unit/parsing/test_warning_collector.py -q
```

预期：因 `BoundedWarningCollector` 不存在而导入失败。

- [ ] **步骤 3：实现有界 warning collector 和设置**

在 `rag_modules/parsing/warnings.py` 中按上述内容实现精确的泛型协议和语义。添加以下正数 Pydantic 字段：

```python
ParserSettings.max_warnings_per_document = Field(default=100, ge=1)
ParserSettings.max_formula_warning_samples = Field(default=5, ge=1, le=100)
PreviewSettings.max_warnings = Field(default=100, ge=1)
```

摘要 warning 不包含源内容。

- [ ] **步骤 4：添加公式聚合 RED 测试**

生成含标题行和至少 1,000 个缺失缓存公式的 XLSX 工作表。断言该表解析后恰好产生一个 `FORMULA_CACHE_UNAVAILABLE` warning，`count == 1000`，样例坐标有序且数量为 5，元数据不含公式文本，warning 总数有上限。添加双工作表覆盖，证明聚合按工作表进行，且文档 collector 会确定性地截断不同 warning 类别。

- [ ] **步骤 5：运行公式 warning 的 RED 测试**

运行：

```bash
.venv/bin/python -m pytest tests/unit/parsing/test_tabular.py \
  -k 'formula and warning' -q
```

预期：因当前解析器为每个公式追加一个 warning 而失败。

- [ ] **步骤 6：实现 XLSX 公式聚合**

修改 `_worksheet_rows`，使其返回或更新一个本地聚合对象，其中包含数量和按配置保留的首批坐标样例。工作表处理完成后发出一个 warning。隐藏、空表、公式和其他解析器 warning 都通过 `BoundedWarningCollector(settings.parser.max_warnings_per_document)` 处理，不保留超限项。

- [ ] **步骤 7：添加预览 warning 上限 RED 测试**

在 `tests/unit/services/test_preview_service.py` 中，让多个已解析文档和分段结果产生多于 `PreviewSettings.max_warnings` 的 warning，但不创建大型负载。断言响应顺序与文档顺序一致，结果长度不超过配置上限，最后一个 warning 为 `WARNINGS_TRUNCATED`，且 `omitted_count` 包含折叠后的解析器摘要和预览级省略项。确认 chunks/documents/完整 `total_chunks` 不受影响。

- [ ] **步骤 8：运行预览 warning RED 测试**

运行：

```bash
.venv/bin/python -m pytest tests/unit/services/test_preview_service.py \
  -k 'warning' -q
```

预期：因 `PreviewService` 当前使用无界列表进行扩展而失败。

- [ ] **步骤 9：实现响应级 warning 限制**

在预览编排期间使用 collector。只有 warning 能被保留时，才将各 parser/segmenter warning 转换为带文档信息的 `PreviewWarning`；预览截断摘要使用安全的合成文档标识，例如空 `document_id`/`filename`，包含稳定的 code/message，并且只带 `omitted_count`。不要把 chunk 截断计为 warning。

- [ ] **步骤 10：运行任务 2 测试和回归测试**

运行：

```bash
.venv/bin/python -m pytest \
  tests/unit/parsing/test_warning_collector.py \
  tests/unit/parsing/test_tabular.py \
  tests/unit/services/test_preview_service.py -q
```

预期：所有 warning、公式、表格和预览 service 测试通过。

- [ ] **步骤 11：追加任务 2 回顾记录**

将 warning 扇出症状、内存/响应影响、collector 语义、任何被否决的实现、RED/GREEN 证据、待创建的提交和剩余风险追加到受跟踪的回顾记录。不要重写早期条目。

- [ ] **步骤 12：提交任务 2**

```bash
git add rag_modules/config/settings.py rag_modules/parsing/warnings.py \
  rag_modules/parsing/__init__.py rag_modules/parsing/xlsx_parser.py \
  rag_modules/services/preview_service.py \
  tests/unit/config/test_settings.py tests/unit/parsing/test_warning_collector.py \
  tests/unit/parsing/test_tabular.py tests/unit/services/test_preview_service.py \
  docs/superpowers/retrospectives/2026-09-03-dataset-indexing-issues.md
git commit -m "fix: bound parser and preview warnings"
```

### 任务 3：分段 CPU 预算与分隔符保真度

**接口：**

- 扩展：`Segmenter(..., max_boundary_scan_characters: int = 100_000_000)`。
- 产出：请求本地工作状态，跟踪普通及父子分段期间剩余的输出记录预算和边界扫描字符预算。
- 产出：预计迭代或扫描字符即将超出预算时，返回稳定的 `SegmentationConfigError(code="SEGMENTATION_LIMIT_EXCEEDED")`。
- 产出：仅当病态情况导致无法生成任何包含分隔符的非空父块时，返回 `SEGMENT_DELIMITER_OMITTED`，消息为 `A source delimiter could not be retained within the configured parent length.`，元数据为 `{"delimiter": "\\n\\n", "count": int}`。

- [ ] **步骤 1：编写失败的预计工作量测试**

在 `tests/unit/segmentation/test_segmenters.py` 中添加测试，用计数型快速失败 spy monkeypatch `_boundary_end`。对于 5,000,000 字符、`maximum=1_000_000`、`overlap=999_999` 和 `max_segments=10_000` 的源文本，断言 `segment()` 在调用 spy 前抛出 `SEGMENTATION_LIMIT_EXCEEDED`。在父子模式下覆盖同样极端的子块配置。

添加使用较小 `max_boundary_scan_characters` 的测试，证明多个源块及父子模式下子块的累计扫描共用一个请求级预算。添加边界推进回归，证明推进量小于 `max(1, ceil((maximum - overlap) / 2))` 的早期首选分隔符会回退到硬性最大长度，而较靠后的首选分隔符仍会优先采用。

- [ ] **步骤 2：运行工作预算 RED 测试**

运行:

```bash
.venv/bin/python -m pytest tests/unit/segmentation/test_segmenters.py \
  -k 'projected or boundary_scan or extreme_overlap' -q
```

预期：由于现有代码只限制输出分段数量，将出现构造函数不匹配，或边界 spy 被调用。

- [ ] **步骤 3：实现预计和实测工作量统计**

为每次 `segment()` 调用创建私有请求级工作预算对象。计算 `hard_advance = maximum - overlap` 和 `minimum_advance = max(1, (hard_advance + 1) // 2)`。仅当 `boundary_end - overlap - start >= minimum_advance` 时接受首选边界，否则使用硬性最大长度。`_split_ranges` 开始前，如果源文本超过一个块，则用整数运算计算已证明的最坏迭代次数 `1 + ceil((len(text) - maximum) / minimum_advance)`，并与剩余记录预算比较，超限即拒绝。预计值只用于拒绝检查；实际父/子/普通记录输出仍由权威记录计数器负责，不得重复扣减预留值。每次边界搜索前按窗口长度扣减 `max_boundary_scan_characters`；如果会超过上限，则在扫描前失败。

重构 `_boundary_end`，调用 `text.rfind(choice, start, limit)`，并在不分配 `text[start:limit]` 的情况下验证候选边界前缀含有非空白内容。保留边界优先级和精确的重叠行为。绝不存储全部范围。

- [ ] **步骤 4：添加失败的分隔符保真度测试**

恢复并加强 `parent_max_length=8` 下的文本/代码/文本和文本/表格/文本测试：父块拼接内容必须等于 `aa\n\n<atomic>\n\nbb`，每个父/子块都非空且不超过上限，可容纳的原子内容保持完整，metadata 可追溯，链接/位置连续，且不发出保真度 warning。

添加 `parent_max_length=1` 和 `=2` 的场景，证明流程有界终止、不产生空分段，并在无法精确保留时聚合为一个 `SEGMENT_DELIMITER_OMITTED` warning。

- [ ] **步骤 5：运行分隔符 RED 测试**

运行：

```bash
.venv/bin/python -m pytest tests/unit/segmentation/test_segmenters.py \
  -k 'delimiter or fallback_preserves' -q
```

预期：普通内容重建失败，因为当前回退逻辑会丢弃仅含分隔符的源内容。

- [ ] **步骤 6：实现感知容量的分隔符附加**

按源顺序构建回退父级源内容，同时保留原子源。若可容纳，将每个真实分隔符附加到前一个非空、非原子源；否则尝试附加到后一个非空、非原子源。两侧都无法容纳时，仅在必要情况下拆分相邻的非原子源内容，使分隔符与非空文本共处于一个父块中。绝不只为保留分隔符而修改或拆分可容纳的原子源。只有当两侧相邻源均为可容纳的原子/满容量源，且不存在既含非空文本又保留原子约定的分隔符父块时，才省略分隔符并递增一次保真 warning 计数。

不发布仅含分隔符的源内容。保持可容纳代码/表格块的原子性，让父记录排在子记录之前，并在省略后重新计算连续位置。

- [ ] **步骤 7：运行任务 3 测试和回归测试**

运行：

```bash
.venv/bin/python -m pytest tests/unit/segmentation tests/unit/services/test_preview_service.py -q
```

预期：配置、进度、硬切分、重叠、保真度、表格、父级链接、warning 和预览错误映射测试全部通过。

- [ ] **步骤 8：追加任务 3 回顾记录**

将极端重叠复杂度测量、输出预算与 CPU 预算的区别、分隔符保真度回归、失败/范围过宽的方案、最终算法、RED/GREEN 证据、待创建提交和剩余风险追加到受跟踪的回顾记录。

- [ ] **步骤 9：提交任务 3**

```bash
git add rag_modules/segmentation/segmenter.py \
  tests/unit/segmentation/test_segmenters.py \
  tests/unit/services/test_preview_service.py \
  docs/superpowers/retrospectives/2026-09-03-dataset-indexing-issues.md
git commit -m "fix: bound segmentation work and preserve delimiters"
```

### 任务 4：YAML 事件预检与稳定的递归处理

**接口：**

- 产出: `_preflight_front_matter(raw: str) -> None`, scanning safe YAML events without constructing values.
- 依赖: existing limits `_MAX_FRONT_MATTER_DEPTH=20` and `_MAX_FRONT_MATTER_NODES=10_000`.
- 对不安全或无法解析的 front matter，在 `front_matter_raw` 中最多保留 `_MAX_FRONT_MATTER_RAW_CHARACTERS=65_536` 个字符。

- [ ] **步骤 1：编写失败的深层 YAML 测试**

添加块状和流式 front matter fixture，至少包含 1,500 层嵌套序列/映射且不含别名或锚点。断言 `MarkdownParser.parse()` 正常返回正文块，暴露有界的 `front_matter_raw`，并且不抛出 `RecursionError`。

添加浅层但超过 10,000 个事件的事件计数覆盖，并分别 monkeypatch `yaml.parse`、`yaml.load` 和 `_normalize_front_matter` 使其抛出 `RecursionError`；所有路径都必须安全回退。保留普通嵌套映射/列表、日期、格式错误 YAML、精确行首分隔符、别名/锚点拒绝、重复键和非有限浮点数的回归。

- [ ] **步骤 2：运行 YAML RED 测试**

运行：

```bash
.venv/bin/python -m pytest tests/unit/parsing/test_text_markdown.py \
  -k 'deep or recursion or event_budget' -q
```

预期：深层 fixture 泄漏 `RecursionError`，或预检函数不存在。

- [ ] **步骤 3：实现事件预检**

使用 `yaml.parse(raw, Loader=yaml.SafeLoader)`，并用深度计数器统计集合开始/结束事件。拒绝深度超过 20、事件总数超过 10,000、别名、无效负深度、最终深度不平衡、YAML 错误和 `RecursionError`。保留现有 token 锚点/别名拒绝作为纵深防御。

为扫描、`_BoundedSafeLoader` 构造/加载和 `_normalize_front_matter` 包装异常处理，使 `RecursionError`、`TypeError`、`ValueError` 和 `yaml.YAMLError` 都选择有界原文回退。不要捕获 `BaseException`、取消、内存耗尽或无关的程序错误。

- [ ] **步骤 4：运行任务 4 测试和解析回归**

运行：

```bash
.venv/bin/python -m pytest tests/unit/parsing/test_text_markdown.py tests/unit/parsing/test_registry.py -q
```

预期：UTF-8/UTF-16 不确定性、文本行、Markdown 标题/列表/表格/代码、front matter 和 registry 测试全部通过。

- [ ] **步骤 5：追加任务 4 回顾记录**

将深层 YAML 复现、构造后再检查深度为何太迟、稳定回退边界、RED/GREEN 证据、待创建提交和剩余风险追加到受跟踪的回顾记录。

- [ ] **步骤 6：提交任务 4**

```bash
git add rag_modules/parsing/markdown_parser.py \
  tests/unit/parsing/test_text_markdown.py \
  docs/superpowers/retrospectives/2026-09-03-dataset-indexing-issues.md
git commit -m "fix: preflight markdown front matter"
```

## 阶段验证

从最终提交的工作树重新运行：

```bash
.venv/bin/python -m pytest \
  tests/unit/config/test_settings.py \
  tests/unit/parsing \
  tests/unit/segmentation \
  tests/unit/services/test_preview_service.py \
  tests/api/test_indexing_preview_api.py \
  tests/unit/object_storage \
  tests/unit/repositories/test_document_repository.py -q
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q rag_modules main.py tests
git diff --check
git status --short
```

预期结果：

- 五项遗留问题均有针对 `c567afc`/`8fb64f4` 行为的 RED 证据和通过的 GREEN 回归覆盖；
- 关系目标、合并范围、公式 warning 扇出、极端重叠、真实分隔符或深层 YAML fixture 均不能绕过边界；
- parser 或 Segmenter 错误均不暴露源内容；
- 预览保持只读，不调用 Embedding/Milvus；
- 受跟踪的回顾记录包含最早期项目问题，以及每项整改任务的问题发现、失败方案、结论、提交和最新验证结果；
- 完整后端套件通过，仅允许有明确记录的基础设施限制跳过项或已有 warning；
- 编译和空白检查退出码为零，且工作树干净。
