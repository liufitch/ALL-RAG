# Parsing and Preview Safety Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the five residual XLSX, warning, segmentation, delimiter-fidelity, and YAML resource-safety findings before the Embedding/Milvus phase begins.

**Architecture:** Keep the public parser, `Segmenter`, and preview interfaces unchanged. Strengthen their internal safety boundaries with relationship-resolved OOXML preflight, bounded warning collectors, projected plus measured segmentation work budgets, fidelity-preserving delimiter attachment, and YAML event preflight before object construction.

**Tech Stack:** Python 3.11, FastAPI, Pydantic v2, AnyIO, PyYAML, lxml, OpenPyXL, pytest.

**Spec:** `docs/superpowers/specs/2026-09-03-parsing-preview-safety-hardening-design.md`

## Global Constraints

- Preview and the future indexing Worker must continue using the same parser registry and `Segmenter` implementation.
- A request timeout is defense in depth; every abandoned synchronous parser or segmenter operation must have a finite input-derived work bound.
- XLSX relationship targets must not escape the OOXML package, use an external target, bypass preflight by filename casing, or reach OpenPyXL before every worksheet part passes preflight.
- Formulas are never evaluated. Cached values take precedence; missing caches retain formula text and emit bounded aggregate warnings without formula contents.
- Public general, parent, and child segments always satisfy `content.strip() != ""`; ordinary full-document parent concatenation preserves real `"\n\n"` boundaries exactly.
- YAML anchors, aliases, excessive nesting, excessive events, recursion, duplicate keys, non-JSON values, non-finite floats, and unbounded scalars never enter document metadata.
- Resource and malformed-input errors expose stable safe codes/messages and never include source contents, relationship targets, formulas, backend exception strings, credentials, or full metadata.
- Every behavior change follows RED-GREEN TDD against commit `c567afc` (whose production behavior is the reviewed `8fb64f4` implementation plus this remediation design document).
- Every task appends its encountered symptoms, root cause, failed approaches, ruling, fix commit, RED/GREEN commands, and remaining risk to `docs/superpowers/retrospectives/2026-09-03-dataset-indexing-issues.md`; this tracked retrospective is the durable record and must never be replaced by the ignored SDD ledger.
- Do not add OCR, `.doc`, formula evaluation, new file formats, new API routes, persistence, Embedding, Milvus, Celery, frontend changes, or unrelated refactors.

---

## File Structure

- Modify: `rag_modules/config/settings.py` — merged-range, parser-warning, preview-warning, and segmentation work budgets.
- Modify: `rag_modules/parsing/xlsx_parser.py` — relationship-resolved worksheet discovery, worksheet XML/merge preflight, formula warning aggregation, isolated physical-cell adapter.
- Create: `rag_modules/parsing/warnings.py` — bounded warning collector shared by parser and preview serialization.
- Modify: `rag_modules/parsing/__init__.py` — export the bounded warning collector only if consumers require a public import.
- Modify: `rag_modules/services/preview_service.py` — response-level warning collector.
- Modify: `rag_modules/segmentation/segmenter.py` — projected iterations, measured boundary scans, no-copy boundary lookup, delimiter attachment and fidelity warning.
- Modify: `rag_modules/parsing/markdown_parser.py` — YAML event preflight and recursion normalization.
- Modify: `tests/unit/config/test_settings.py` — positive/default budget parsing and invalid values.
- Modify: `tests/unit/parsing/test_tabular.py` — adversarial relationship, merge, sparse-cell, formula aggregation, and warning bounds.
- Create: `tests/unit/parsing/test_warning_collector.py` — exact bounded collector behavior.
- Modify: `tests/unit/services/test_preview_service.py` — cross-document preview warning cap.
- Modify: `tests/unit/segmentation/test_segmenters.py` — projected/measured work limits and exact ordinary fallback reconstruction.
- Modify: `tests/unit/parsing/test_text_markdown.py` — deep block/flow YAML and regression coverage.
- Modify: `docs/superpowers/retrospectives/2026-09-03-dataset-indexing-issues.md` — append-only review record for every task and review finding.

### Task 1: Relationship-Resolved XLSX Preflight and Merge Bounds

**Interfaces:**

- Produces: immutable `_WorksheetPreflight(title: str, part_name: str, physical_coordinates: frozenset[tuple[int, int]])` records in workbook sheet order.
- Produces: `_worksheet_parts(archive: ZipFile) -> tuple[tuple[str, str], ...]` resolving each workbook sheet title/relationship to its exact canonical package member path. Accept only the Transitional URI `http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet` and Strict URI `http://purl.oclc.org/ooxml/officeDocument/relationships/worksheet`; suffix matching is forbidden.
- Produces: `_preflight_worksheet_xml(payload: bytes) -> tuple[_WorksheetPreflight, ...]` that validates only those resolved worksheet parts before `load_workbook` runs and retains their bounded physical coordinate sets.
- Produces: an isolated `_physical_cells(worksheet, cached_worksheet, physical_coordinates) -> Iterator[tuple[int, int, Any, Any]]` adapter that yields formula/cached physical non-placeholder cells without expanding declared dimensions, creating cached cells, or accepting merged placeholders.
- Consumes: `settings.parser.max_spreadsheet_xml_nodes`, `max_physical_cells`, `max_row_coordinate`, `max_column_coordinate`, `max_merged_cell_area`, and `max_total_merged_cell_area`.

**Exact defaults and errors:**

- `ParserSettings.max_merged_cell_area = Field(default=100_000, ge=1)`.
- `ParserSettings.max_total_merged_cell_area = Field(default=1_000_000, ge=1)`; a model validator requires total greater than or equal to the single-range limit and less than or equal to `max_physical_cells`.
- Relationship/package corruption: `XLSX_MALFORMED`, message `The XLSX file is malformed or unreadable.`
- Single merge limit: `TABLE_MERGED_CELL_LIMIT_EXCEEDED`, message `A merged spreadsheet range exceeds the configured area limit.`
- Aggregate merge limit: `TABLE_TOTAL_MERGED_CELL_LIMIT_EXCEEDED`, message `The spreadsheet exceeds the configured merged-cell area limit.`

- [ ] **Step 1: Add failing settings and relationship tests**

Add tests to `tests/unit/config/test_settings.py` for the exact defaults, positive-value validation, `total >= single`, and `total <= max_physical_cells`.

Extend the test ZIP helper in `tests/unit/parsing/test_tabular.py` so it can rename a worksheet member and update `xl/_rels/workbook.xml.rels`. Add RED tests proving:

1. a relationship target renamed from `worksheets/sheet1.xml` to `worksheets/sheet1.XML` is still preflighted and exceeds `max_physical_cells=2` when it contains three cells;
2. `TargetMode="External"`, an absolute target, `../` traversal, missing target, duplicate worksheet target, and a non-worksheet relationship type all raise `XLSX_MALFORMED`;
3. monkeypatching `rag_modules.parsing.xlsx_parser.load_workbook` to fail proves it is never called for a rejected package.

- [ ] **Step 2: Run the relationship RED tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/unit/config/test_settings.py \
  tests/unit/parsing/test_tabular.py \
  -k 'merged_cell or relationship or case_varied or external or traversal or missing_target or duplicate_target' -q
```

Expected: failures because the new settings do not exist and the current glob skips case-varied or relationship-invalid parts.

- [ ] **Step 3: Implement canonical relationship discovery**

Parse `xl/workbook.xml` and `xl/_rels/workbook.xml.rels` with lxml using `resolve_entities=False`, `no_network=True`, and `huge_tree=False`. Match workbook sheet `r:id` values to unique internal worksheet relationships and reject duplicate relationship IDs. Resolve targets relative to `xl/workbook.xml` with `PurePosixPath`; reject empty segments, absolute paths, URLs, query/fragment text, percent escapes, backslashes, `..`, external mode, missing members, duplicate resolved members, and any relationship type other than the exact Transitional or Strict worksheet URI listed in the interface.

Never use lower-casing to open a ZIP member. Return the exact resolved member paths and stream-preflight every one before either OpenPyXL workbook is created.

- [ ] **Step 4: Add failing merged-range tests**

Use `_xlsx_bytes` to patch `sheet1.xml` with:

- one real `A1` cell plus `<mergeCell ref="A1:Z100"/>` under a small per-range limit;
- several individually valid ranges whose aggregate area crosses the total limit;
- malformed, reversed, out-of-sheet and missing merge references.

Monkeypatch `load_workbook` with a fail-fast function and assert every breach occurs before OpenPyXL. Add a normal `A1:B2` merged workbook regression proving parsing remains bounded and does not emit `MergedCell` placeholders as values.

- [ ] **Step 5: Run merged-range RED tests**

Run:

```bash
.venv/bin/python -m pytest tests/unit/parsing/test_tabular.py \
  -k 'merge or merged or physical_cell_adapter' -q
```

Expected: failures because current preflight ignores `<mergeCell>` area and normal-mode `_cells` may contain materialized merged placeholders.

- [ ] **Step 6: Implement merge preflight and physical-cell adapter**

During worksheet `iterparse`, validate every `<mergeCell ref>` with `openpyxl.utils.cell.range_boundaries`, integer area arithmetic, XLSX coordinate maxima, the configured single/aggregate limits, and a set preventing duplicate range accounting. Preserve the existing node/cell/non-empty-coordinate limits.

Move all `_cells` access into `_physical_cells`. Fail with `XLSX_MALFORMED` when the expected mapping contract is unavailable, when OpenPyXL sheet title/order differs from preflight, or when a cached/formula mapping contains an impossible unexpected physical coordinate. Iterate the sorted preflight coordinate set and use mapping `.get()` only. Yield only real physical `Cell` values; skip `MergedCell` placeholders and coordinates absent from the worksheet XML physical-cell set captured by preflight. Do not call `worksheet.cell()` to enumerate source or cached cells.

- [ ] **Step 7: Run Task 1 tests and regressions**

Run:

```bash
.venv/bin/python -m pytest tests/unit/config/test_settings.py tests/unit/parsing/test_tabular.py -q
```

Expected: all relationship, merge, declared-dimension, sparse-row, formula, hidden/empty sheet, table-limit and malformed-file tests pass.

- [ ] **Step 8: Append the Task 1 retrospective record**

Append Task 1 evidence to `docs/superpowers/retrospectives/2026-09-03-dataset-indexing-issues.md`, including every relationship/merge symptom, root cause, rejected approach, final design, RED/GREEN command and result, commit-to-be-created, and residual risk. Do not rewrite earlier entries.

- [ ] **Step 9: Commit Task 1**

```bash
git add rag_modules/config/settings.py rag_modules/parsing/xlsx_parser.py \
  tests/unit/config/test_settings.py tests/unit/parsing/test_tabular.py \
  docs/superpowers/retrospectives/2026-09-03-dataset-indexing-issues.md
git commit -m "fix: bound xlsx worksheet materialization"
```

### Task 2: Bounded Parser and Preview Warnings

**Interfaces:**

- Produces: generic `BoundedWarningCollector[T](limit: int, summary_factory: Callable[[int], T])` with `add(warning: T)`, `extend(iterable: Iterable[T])`, and `result() -> tuple[T, ...]`; `T` satisfies a small `WarningLike` protocol exposing `code: str` and `metadata: dict[str, Any]`, so the same collector supports both `ParserWarning` and `PreviewWarning` without converting away document identity.
- Produces: one per-sheet `FORMULA_CACHE_UNAVAILABLE` warning with metadata `{"sheet": str, "count": int, "sample_cells": list[str]}` and at most five samples.
- Produces: `WARNINGS_TRUNCATED`, message `Additional warnings were omitted.`, metadata `{"omitted_count": int}`.
- Consumes: `settings.parser.max_warnings_per_document=100`, `settings.parser.max_formula_warning_samples=5`, and `settings.preview.max_warnings=100`.

**Collector semantics:**

- `limit` includes the truncation summary.
- For `limit=1`, the result is only `WARNINGS_TRUNCATED` with the total omitted count.
- For `limit>1`, retain the first `limit - 1` warnings and summarize all remaining warnings in the final slot.
- The collector never retains omitted warning objects or their metadata.
- Adding a preexisting `WARNINGS_TRUNCATED` warning adds its positive integer `omitted_count` to the collector's omitted total rather than nesting summaries. The caller-provided `summary_factory(omitted_count)` creates either a `ParserWarning` or document-neutral `PreviewWarning` as appropriate.

- [ ] **Step 1: Write failing collector tests**

Create `tests/unit/parsing/test_warning_collector.py` covering empty input, below-limit order, exact-limit behavior, limit 1, over-limit counts, chained `extend`, preexisting summary folding, invalid non-positive limit, and a generator whose warning metadata is released after each omitted item.

- [ ] **Step 2: Run collector tests RED**

Run:

```bash
.venv/bin/python -m pytest tests/unit/parsing/test_warning_collector.py -q
```

Expected: import failure because `BoundedWarningCollector` does not exist.

- [ ] **Step 3: Implement the bounded warning collector and settings**

Create `rag_modules/parsing/warnings.py` with the exact generic protocol and semantics above. Add positive Pydantic fields:

```python
ParserSettings.max_warnings_per_document = Field(default=100, ge=1)
ParserSettings.max_formula_warning_samples = Field(default=5, ge=1, le=100)
PreviewSettings.max_warnings = Field(default=100, ge=1)
```

The summary warning contains no source contents.

- [ ] **Step 4: Add formula aggregation RED tests**

Generate an XLSX sheet with a header and at least 1,000 missing-cache formulas. Assert parsing yields exactly one `FORMULA_CACHE_UNAVAILABLE` warning for that sheet, `count == 1000`, five ordered cell-coordinate samples, no formula text in metadata, and bounded total warnings. Add two-sheet coverage proving aggregation is per sheet and the document collector truncates distinct warning categories deterministically.

- [ ] **Step 5: Run formula-warning RED tests**

Run:

```bash
.venv/bin/python -m pytest tests/unit/parsing/test_tabular.py \
  -k 'formula and warning' -q
```

Expected: failure because the current parser appends one warning per formula.

- [ ] **Step 6: Implement XLSX formula aggregation**

Change `_worksheet_rows` to return or update one local aggregate containing a count and the first configured sample coordinates. Emit one warning after the sheet has been consumed. Route hidden, empty, formula and other parser warnings through `BoundedWarningCollector(settings.parser.max_warnings_per_document)` without retaining overflow.

- [ ] **Step 7: Add preview warning-cap RED tests**

In `tests/unit/services/test_preview_service.py`, make multiple parsed documents and segmentation results yield more than `PreviewSettings.max_warnings` without large payloads. Assert response order is document order, result length never exceeds the configured limit, the final warning is `WARNINGS_TRUNCATED`, and `omitted_count` includes folded parser summaries plus preview-level omissions. Confirm chunks/documents/full `total_chunks` are unaffected.

- [ ] **Step 8: Run preview warning RED tests**

Run:

```bash
.venv/bin/python -m pytest tests/unit/services/test_preview_service.py \
  -k 'warning' -q
```

Expected: failure because `PreviewService` currently extends an unbounded list.

- [ ] **Step 9: Implement response-level warning bounding**

Use a collector during preview orchestration. Convert each parser/segmenter warning to its document-qualified `PreviewWarning` only while it can be retained; the preview truncation summary uses a safe synthetic document identity such as empty `document_id`/`filename`, stable code/message, and only `omitted_count`. Do not count chunk truncation as a warning.

- [ ] **Step 10: Run Task 2 tests and regressions**

Run:

```bash
.venv/bin/python -m pytest \
  tests/unit/parsing/test_warning_collector.py \
  tests/unit/parsing/test_tabular.py \
  tests/unit/services/test_preview_service.py -q
```

Expected: all warning, formula, tabular and preview-service tests pass.

- [ ] **Step 11: Append the Task 2 retrospective record**

Append warning fan-out symptoms, memory/response impact, collector semantics, any rejected implementation, RED/GREEN evidence, commit-to-be-created, and residual risk to the tracked retrospective. Do not rewrite earlier entries.

- [ ] **Step 12: Commit Task 2**

```bash
git add rag_modules/config/settings.py rag_modules/parsing/warnings.py \
  rag_modules/parsing/__init__.py rag_modules/parsing/xlsx_parser.py \
  rag_modules/services/preview_service.py \
  tests/unit/config/test_settings.py tests/unit/parsing/test_warning_collector.py \
  tests/unit/parsing/test_tabular.py tests/unit/services/test_preview_service.py \
  docs/superpowers/retrospectives/2026-09-03-dataset-indexing-issues.md
git commit -m "fix: bound parser and preview warnings"
```

### Task 3: Segmentation CPU Budget and Delimiter Fidelity

**Interfaces:**

- Extends: `Segmenter(..., max_boundary_scan_characters: int = 100_000_000)`.
- Produces: request-local work state tracking remaining emitted records and boundary-scan characters across general and parent-child splitting.
- Produces: stable `SegmentationConfigError(code="SEGMENTATION_LIMIT_EXCEEDED")` before projected iterations or scan characters exceed their budgets.
- Produces: `SEGMENT_DELIMITER_OMITTED`, message `A source delimiter could not be retained within the configured parent length.`, with metadata `{"delimiter": "\\n\\n", "count": int}` only for pathological cases where no nonblank delimiter-bearing parent is possible.

- [ ] **Step 1: Write failing projected-work tests**

Add tests to `tests/unit/segmentation/test_segmenters.py` that monkeypatch `_boundary_end` with a counting fail-fast spy. For a 5,000,000-character source with `maximum=1_000_000`, `overlap=999_999`, and `max_segments=10_000`, assert `segment()` raises `SEGMENTATION_LIMIT_EXCEEDED` before the spy is called. Cover the same extreme child configuration in parent-child mode.

Add tests using a small `max_boundary_scan_characters` to prove cumulative scans across multiple source blocks and parent-child children consume one request-wide budget. Add a boundary-progress regression proving an early preferred delimiter that would advance less than `max(1, ceil((maximum - overlap) / 2))` falls back to the hard maximum, while a later preferred delimiter still wins.

- [ ] **Step 2: Run work-budget tests RED**

Run:

```bash
.venv/bin/python -m pytest tests/unit/segmentation/test_segmenters.py \
  -k 'projected or boundary_scan or extreme_overlap' -q
```

Expected: constructor mismatch or the boundary spy is called because the existing code only caps emitted segments.

- [ ] **Step 3: Implement projected and measured work accounting**

Introduce a private request-local work-budget object created per `segment()` call. Compute `hard_advance = maximum - overlap` and `minimum_advance = max(1, (hard_advance + 1) // 2)`. Accept a preferred boundary only when `boundary_end - overlap - start >= minimum_advance`; otherwise use the hard maximum. Before `_split_ranges` begins, compute the proven worst-case iteration count `1 + ceil((len(text) - maximum) / minimum_advance)` using integer arithmetic when the source exceeds one chunk, and reject it against the remaining record budget. Projection is a rejection check; actual parent/child/general record emission still owns the authoritative record counter and must not double-decrement a reservation. Before each boundary search, charge the window length against `max_boundary_scan_characters`; fail before scanning when it would exceed the cap.

Refactor `_boundary_end` to call `text.rfind(choice, start, limit)` and validate that the prefix before the candidate contains non-whitespace without allocating `text[start:limit]`. Preserve boundary priority and exact overlap behavior. Never store all ranges.

- [ ] **Step 4: Add failing delimiter-fidelity tests**

Restore and strengthen prose/code/prose and prose/table/prose tests for `parent_max_length=8`: concatenated parent contents must equal `aa\n\n<atomic>\n\nbb`, every parent/child must be nonblank and within its maximum, fitting atomic content remains intact, metadata remains traceable, links/positions are contiguous, and no fidelity warning is emitted.

Add `parent_max_length=1` and `=2` cases proving bounded termination, no blank segment, and one aggregated `SEGMENT_DELIMITER_OMITTED` warning when exact retention is impossible.

- [ ] **Step 5: Run delimiter tests RED**

Run:

```bash
.venv/bin/python -m pytest tests/unit/segmentation/test_segmenters.py \
  -k 'delimiter or fallback_preserves' -q
```

Expected: ordinary reconstruction fails because the current fallback drops delimiter-only sources.

- [ ] **Step 6: Implement capacity-aware delimiter attachment**

Build fallback parent sources in source order while preserving atomic sources. Attach each real delimiter to the preceding nonblank non-atomic source when it fits; otherwise to the following nonblank non-atomic source. When neither fits, split adjacent non-atomic source content only as needed so the delimiter shares a parent with nonblank text. Never modify or split a fitting atomic source merely to retain a delimiter. Omit the delimiter and increment one fidelity-warning count only when both adjacent fitting atomic/full-capacity sources leave no nonblank delimiter-bearing parent that also preserves the atomic contract.

Do not publish delimiter-only sources. Keep fitting code/table blocks atomic, keep parent records before their children, and recalculate contiguous positions after omissions.

- [ ] **Step 7: Run Task 3 tests and regressions**

Run:

```bash
.venv/bin/python -m pytest tests/unit/segmentation tests/unit/services/test_preview_service.py -q
```

Expected: all configuration, progress, hard-split, overlap, fidelity, table, parent-link, warning and preview error-mapping tests pass.

- [ ] **Step 8: Append the Task 3 retrospective record**

Append the extreme-overlap complexity measurements, distinction between output and CPU budgets, delimiter-fidelity regression, failed/over-broad approach, final algorithm, RED/GREEN evidence, commit-to-be-created, and residual risk to the tracked retrospective.

- [ ] **Step 9: Commit Task 3**

```bash
git add rag_modules/segmentation/segmenter.py \
  tests/unit/segmentation/test_segmenters.py \
  tests/unit/services/test_preview_service.py \
  docs/superpowers/retrospectives/2026-09-03-dataset-indexing-issues.md
git commit -m "fix: bound segmentation work and preserve delimiters"
```

### Task 4: YAML Event Preflight and Stable Recursion Handling

**Interfaces:**

- Produces: `_preflight_front_matter(raw: str) -> None`, scanning safe YAML events without constructing values.
- Consumes: existing limits `_MAX_FRONT_MATTER_DEPTH=20` and `_MAX_FRONT_MATTER_NODES=10_000`.
- On unsafe/unparseable front matter, preserves at most `_MAX_FRONT_MATTER_RAW_CHARACTERS=65_536` characters under `front_matter_raw`.

- [ ] **Step 1: Write failing deep-YAML tests**

Add block-style and flow-style front matter fixtures of at least 1,500 nested sequences/maps that contain no alias or anchor. Assert `MarkdownParser.parse()` returns body blocks normally, exposes bounded `front_matter_raw`, and never raises `RecursionError`.

Add event-count coverage with shallow input over 10,000 events, and monkeypatch `yaml.parse`, `yaml.load`, and `_normalize_front_matter` separately to raise `RecursionError`; all paths must fall back safely. Retain regressions for ordinary nested maps/lists, dates, malformed YAML, exact column-zero delimiters, alias/anchor rejection, duplicate keys and non-finite floats.

- [ ] **Step 2: Run YAML tests RED**

Run:

```bash
.venv/bin/python -m pytest tests/unit/parsing/test_text_markdown.py \
  -k 'deep or recursion or event_budget' -q
```

Expected: a deep fixture leaks `RecursionError`, or the preflight function is absent.

- [ ] **Step 3: Implement event preflight**

Use `yaml.parse(raw, Loader=yaml.SafeLoader)` and count collection-start/end events with a depth counter. Reject depth over 20, total events over 10,000, aliases, invalid negative depth, unbalanced final depth, YAML errors, and `RecursionError`. Keep the existing token anchor/alias rejection as defense in depth.

Wrap scanning, `_BoundedSafeLoader` construction/load, and `_normalize_front_matter` so `RecursionError`, `TypeError`, `ValueError`, and `yaml.YAMLError` all select the bounded raw fallback. Do not catch `BaseException`, cancellation, memory exhaustion, or unrelated programmer errors.

- [ ] **Step 4: Run Task 4 tests and parsing regressions**

Run:

```bash
.venv/bin/python -m pytest tests/unit/parsing/test_text_markdown.py tests/unit/parsing/test_registry.py -q
```

Expected: all UTF-8/UTF-16 uncertainty, text lines, Markdown headings/lists/tables/code, front matter and registry tests pass.

- [ ] **Step 5: Append the Task 4 retrospective record**

Append the deep-YAML reproduction, why post-construction depth checking was too late, stable fallback boundary, RED/GREEN evidence, commit-to-be-created, and residual risk to the tracked retrospective.

- [ ] **Step 6: Commit Task 4**

```bash
git add rag_modules/parsing/markdown_parser.py \
  tests/unit/parsing/test_text_markdown.py \
  docs/superpowers/retrospectives/2026-09-03-dataset-indexing-issues.md
git commit -m "fix: preflight markdown front matter"
```

## Phase Verification

Run fresh from the final committed tree:

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

Expected:

- all five residual findings have RED evidence against `c567afc`/`8fb64f4` behavior and GREEN regression coverage;
- no relationship target, merge range, formula-warning fan-out, extreme overlap, real delimiter, or deep YAML fixture bypasses its boundary;
- no parser or Segmenter error exposes source contents;
- preview remains read-only and does not call Embedding/Milvus;
- the tracked retrospective contains the earliest project issues plus every remediation-task finding, failed approach, ruling, commit and fresh verification result;
- the complete backend suite passes with only explicitly documented infrastructure-gated skips or pre-existing warnings;
- compile and whitespace checks exit zero, and the worktree is clean.
