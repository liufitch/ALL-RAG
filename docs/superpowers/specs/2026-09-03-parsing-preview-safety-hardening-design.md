# Parsing and Preview Safety Hardening Design

**Date:** 2026-09-03

**Status:** Proposed remediation design for the residual findings after the
Parsing, Segmentation, and Preview phase review.

**Parent specification:**
`docs/superpowers/specs/2026-08-31-dify-style-dataset-indexing-design.md`

## 1. Goal

Close the five load-bearing parser and segmentation findings that remain after
commit `8fb64f4`, without changing the approved product workflow or introducing
new persistence, Embedding, or vector-store behavior.

The resulting parser/segmenter must remain shared by preview and the future
indexing Worker. A request deadline may stop waiting for synchronous work, but
every abandoned worker must also have a finite, input-derived work bound.

## 2. Scope

This remediation covers only:

1. XLSX worksheet-part discovery, sparse-cell and merged-range resource bounds.
2. Aggregation and bounding of formula-cache warnings.
3. Segmentation CPU-work prediction and accounting for extreme overlap.
4. Fidelity-preserving, nonblank full-document delimiter handling.
5. YAML nesting preflight and stable normalization of recursion failures.

It does not add OCR, `.doc`, formula evaluation, new file formats, new API
routes, persistence, Embedding, Milvus, Celery, or frontend behavior.

## 3. Considered Approaches

### 3.1 Recommended: strengthen the existing boundaries

- Resolve worksheet parts from OOXML workbook relationships rather than ZIP
  filename patterns.
- Stream-preflight actual worksheet XML and reject excessive nodes, physical
  cells, coordinates, or merged-range area before OpenPyXL materializes cells.
- Retain OpenPyXL for value/type/date semantics after the preflight.
- Reject segmentation configurations whose projected minimum work exceeds a
  fixed budget, and also account for actual boundary-scan characters.
- Attach real delimiters to neighboring nonblank sources instead of publishing
  delimiter-only segments.
- Scan YAML events before object construction and fall back to bounded raw
  metadata on excessive nesting or parser recursion.

This preserves the current interfaces and limits change to the reviewed risk
surfaces.

### 3.2 Reject advanced spreadsheet structures

Reject every workbook containing merged cells, formulas, or unusual worksheet
relationship targets. This is simpler, but rejects ordinary business files and
does not meet the existing formula-cache behavior.

### 3.3 Replace OpenPyXL with a custom OOXML value parser

Parse workbook relationships, shared strings, styles, dates, formulas and cells
directly. This offers the strongest control but duplicates mature spreadsheet
semantics, substantially expands this remediation, and carries higher data-loss
risk. It is not justified for the five residual findings.

## 4. XLSX Resource Boundary

### 4.1 Relationship-driven worksheet discovery

The parser must not infer worksheets by matching ZIP member names such as
`xl/worksheets/*.xml`.

It must:

1. Read `xl/workbook.xml` and `xl/_rels/workbook.xml.rels` using entity-disabled,
   no-network XML parsing.
2. Resolve each workbook sheet relationship whose type is the OOXML worksheet
   relationship type.
3. Normalize each relationship target as a POSIX package path relative to the
   workbook part.
4. Reject absolute paths, URL targets, `TargetMode="External"`, `..` traversal,
   missing parts, duplicate resolved parts and non-worksheet relationship types.
5. Use the resolved part list for every worksheet preflight. ZIP member lookup
   must use the exact relationship target; filename case cannot bypass the
   preflight.

OpenPyXL must not be invoked until all resolved worksheet parts pass preflight.

### 4.2 Streaming XML budget

Across all resolved worksheet parts, enforce configurable positive limits for:

- parsed XML end events;
- physical `<c>` cell elements;
- non-empty row and column coordinates;
- total declared merged-cell area;
- the area of each individual merged range.

A merged range area is `(max_row - min_row + 1) * (max_col - min_col + 1)`.
Area calculation must be overflow-safe and must reject malformed, reversed or
out-of-range references. The check occurs while streaming `<mergeCell ref>`
elements, before OpenPyXL normal-mode workbooks are created.

The default aggregate merge-area budget must be no greater than the configured
physical-cell budget. Large, sparse source coordinates remain rejected using
the existing row/column coordinate limits.

Any budget breach raises a stable `DocumentParseError`; malformed package paths,
relationships or XML normalize to `XLSX_MALFORMED`. No exception message or ZIP
member content is returned to clients.

### 4.3 Post-preflight value reading

After preflight, the parser may continue using paired formula and `data_only`
OpenPyXL workbooks with `keep_links=False`. Access to private `_cells` must be
isolated behind one small adapter function and guarded by a compatibility test;
the adapter must iterate only coordinates backed by a physical cell in the
formula view. Merged placeholders must never be treated as source values.

Formula execution remains forbidden. A cached value is preferred; otherwise
the formula text is retained.

## 5. Warning Aggregation and Preview Bound

The parser must not emit one warning object per formula.

For each worksheet, missing formula caches are aggregated into one
`FORMULA_CACHE_UNAVAILABLE` warning containing:

- `sheet`;
- `count`;
- up to five deterministic cell-coordinate samples in source order.

All parser warnings additionally pass through a document-wide warning
collector with a hard maximum number of warning objects. When distinct warnings
exceed the cap, append or update one stable `WARNINGS_TRUNCATED` summary with the
omitted count. The collector itself must not retain every omitted warning.

Preview serialization applies its own response-level warning cap across all
documents. `PreviewResponse` reports only the bounded list and includes a stable
summary warning when additional parser or segmenter warnings were omitted.
Warnings never contain cell contents, formulas, file bodies, credentials or
backend exception messages.

## 6. Segmentation Work Budget

### 6.1 Preflight projection

Before splitting a source of length `L`, maximum chunk length `M`, and overlap
`O`, compute the minimum guaranteed advance `A = M - O`. Configuration validation
already requires `M > O`.

The worst-case number of iterations for a hard-boundary fallback is:

```text
1                              if L <= M
1 + ceil((L - M) / A)          otherwise
```

Reject before entering the loop when the projected iterations exceed the
remaining segment/work budget. Parent-child mode includes parent records and
all projected child ranges in the same request-wide budget.

Projection must use integer arithmetic and must not allocate ranges or source
substrings.

### 6.2 Boundary-scan accounting

Projection by segment count alone is insufficient because every preferred
boundary search may inspect up to `M` characters. Maintain a cumulative
`boundary_scan_characters` counter, charged by the actual search window length
before `_boundary_end()` runs. Reject with
`SEGMENTATION_LIMIT_EXCEEDED` before the configured scan budget is crossed.

Avoid copying the full search window. Boundary lookup must use `str.rfind`
start/end arguments against the original string. Custom separators longer than
the window remain valid and simply yield no boundary.

The defaults must ensure the maximum accepted request finishes in a bounded
amount of synchronous work even after an HTTP caller stops waiting. The same
limits apply when Segmenter is called directly by the future Worker.

## 7. Nonblank and Fidelity-preserving Delimiters

Public general, parent and child segments must continue satisfying
`content.strip() != ""`.

Real `"\n\n"` boundaries between prose, code and table sources must be retained
when a neighboring nonblank parent has capacity:

- Prefer appending the delimiter to the preceding nonblank source if doing so
  stays within the parent maximum.
- Otherwise prefix it to the following nonblank source if that stays within the
  maximum.
- If neither side can contain the delimiter, split/attach delimiter characters
  only as part of a chunk that also contains nonblank source text.
- Only when the configured maximum makes any nonblank delimiter-bearing chunk
  impossible may the synthetic delimiter be omitted, accompanied by a stable
  fidelity warning.

For ordinary limits, concatenating parent contents must reproduce the original
combined source exactly. Code and table blocks that already fit the maximum
remain atomic. Parent IDs, child links, source order and contiguous public
positions remain deterministic.

## 8. YAML Pre-construction Boundary

Before calling `yaml.load`, scan YAML events with the safe parser and track
collection nesting depth. Reject structural construction when:

- depth exceeds 20;
- event count exceeds the existing node budget;
- any alias or anchor appears;
- scanning raises a YAML parser/scanner error or `RecursionError`.

Only input passing the event preflight may reach `_BoundedSafeLoader` and the
existing JSON-compatible normalizer. `yaml.load`, loader construction and
normalization must all catch `RecursionError` and fall back to at most 65,536
characters of `front_matter_raw`.

The fallback body remains parseable Markdown. Cyclic/shared graphs, Python
objects, non-string mapping keys, duplicate keys, non-finite floats and
unbounded scalars never enter document metadata.

## 9. Error Semantics

- Security/resource-limit rejections use stable, safe `DocumentParseError` or
  `SegmentationConfigError` codes.
- Preview maps segmentation limit failures to the existing stable 422 error
  envelope with `code`, `message`, sanitized `detail`, `request_id` and matching
  `X-Request-ID`.
- Parser corruption and malformed package errors do not expose exception text.
- HTTP timeouts remain defense in depth; correctness does not depend on killing
  a Python worker thread.

## 10. Testing Strategy

Every fix follows RED-GREEN TDD against commit `8fb64f4`.

### 10.1 XLSX

- A workbook relationship targeting a case-varied worksheet part is still
  preflighted and cannot bypass the physical-cell budget.
- Traversal, absolute, external, duplicate and missing relationship targets are
  rejected without invoking OpenPyXL.
- One physical cell plus a large merged range is rejected before `_cells`
  expansion.
- Normal merged cells, cached formulas, missing-cache aggregation, hidden/empty
  sheets, sparse rows and real row numbers remain correct.
- Thousands of missing formula caches yield one bounded aggregate warning, not
  thousands of warning objects.

### 10.2 Segmentation

- Extreme `M=1_000_000, O=999_999` is rejected by preflight without calling
  `_boundary_end()` repeatedly.
- Cumulative boundary-scan accounting rejects work before its cap.
- Normal Chinese/English boundaries, custom separators, overlap reconstruction,
  hard splitting and parent-child links remain unchanged.
- Ordinary prose/code/prose and prose/table/prose parent concatenation preserves
  `"\n\n"` exactly while every public segment stays nonblank.
- Pathological one-character limits return bounded output plus a fidelity
  warning rather than a blank segment.

### 10.3 Markdown and preview

- Deep block and flow YAML fails safely without `RecursionError` escaping.
- Existing nested/list/scalar front matter remains JSON-compatible.
- Multi-document preview warnings remain within the response cap and report a
  deterministic omitted count.
- Full parsing, segmentation, preview, object-storage and repository regression
  suites pass, followed by the complete backend suite, compile check and
  `git diff --check`.

## 11. Acceptance Criteria

The remediation is complete only when:

1. All five scoped review findings have a failing regression test against
   `8fb64f4` and a passing result after implementation.
2. No worksheet part or merged range can bypass pre-materialization limits.
3. No accepted segmentation request can exceed either the projected iteration
   or boundary-scan budget.
4. Ordinary full-document parent reconstruction remains exact and all public
   chunks are nonblank.
5. Deep YAML and warning fan-out cannot create an unhandled exception or
   unbounded response.
6. A fresh independent task review and final whole-remediation review report no
   Critical or Important issue.
7. The full backend test suite, compile check and whitespace check pass on the
   final commit.

