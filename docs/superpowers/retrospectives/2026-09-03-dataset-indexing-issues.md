# Dataset Indexing Issue Retrospective

**Started:** 2026-09-03  
**Scope:** From the earliest reported knowledge-base API problem through the
current parsing/preview safety remediation.  
**Update rule:** Append new incidents and review findings chronologically. Do
not erase superseded rulings; mark them as superseded and explain why. Never
record passwords, API keys, source document bodies, complete vectors, or raw
backend exception payloads.

## How to read this document

Each entry distinguishes its evidence source:

- **User report:** observed in the development environment or supplied during
  requirements discussion; it may predate automated regression coverage.
- **Git/code evidence:** traceable to a repository commit, configuration model,
  route, migration, or implementation diff.
- **Test/review evidence:** reproduced by an automated test, bounded probe, or
  independent code review.

Statuses:

- **Resolved:** implemented, reviewed and verified at the recorded commit.
- **Superseded:** an earlier decision was later disproved or replaced; retained
  here for learning.
- **Open:** confirmed and not yet accepted by independent review.
- **Design/plan:** implementation has not started.

---

## 1. Earliest knowledge-base API and configuration issues

### KB-001 — Knowledge-base list request returned 404

- **Source:** User report: `GET /api/knowledge_base/list?status=all&visibility=all`
  returned `404 Not Found`.
- **Impact:** The frontend could not load the knowledge-base list and initially
  appeared disconnected from backend persistence.
- **Root-cause direction:** The original route set and frontend expectations
  were not aligned. The early codebase evolved from a local/tutorial-style
  implementation rather than a stable dataset API contract.
- **Decision:** The new API uses PostgreSQL-backed `datasets` domain records and
  dedicated knowledge-base/document routers. Compatibility must be verified
  through API route tests rather than by adding another JSON fallback.
- **Evidence:** Early frontend commits `a34a3bd` and `044098e`; architecture and
  implementation plans in commits `4739752` and `7cef002`; dataset creation in
  `f134257`.
- **Evidence:** The current `rag_modules/api/knowledge_base_api.py` registers
  `GET /api/knowledge_base/list`; `tests/test_api_routes.py` asserts the exact
  frontend path is present in OpenAPI, and `tests/api/test_dataset_api.py`
  exercises list behavior.
- **Status:** Resolved by the PostgreSQL-backed API route and contract tests.
- **Lesson:** Freeze the frontend/backend route contract before implementing
  persistence. A 404 should trigger route inventory and contract tests, not a
  second data source.

### KB-002 — Obsolete `data/knowledge_base.json` / `knowledge_bases.json`

- **Source:** User report and explicit request to delete the unused JSON file.
- **Impact:** A stale local JSON path obscured the real source of truth and made
  it unclear whether PostgreSQL or a file supplied knowledge-base data.
- **Root cause:** Tutorial/local persistence remained adjacent to the new API
  and PostgreSQL design.
- **Decision:** PostgreSQL is the only business metadata source of truth. The
  old JSON and its `main.py` references must not be reintroduced.
- **Evidence:** Foundation/MinIO ledger records removal of the stale
  `knowledge_bases.json` block from `main.py`; final roadmap acceptance item 30
  keeps a repository-wide cleanup check in phase 7.
- **Status:** Active runtime references were removed; repository-wide absence is
  retained as a final hardening gate.
- **Lesson:** Deleting a file is insufficient unless imports, fallback branches,
  startup code, documentation and tests are scanned together.

### CFG-001 — PostgreSQL authenticated as unexpected user `fitch`

- **Source:** User report: `asyncpg.exceptions.InvalidPasswordError: password
  authentication failed for user "fitch"`.
- **Impact:** The application could not establish its configured PostgreSQL
  connection, and it was unclear where the username/password originated.
- **Root cause:** The settings model's top-level field is named `database` and
  Pydantic uses `env_nested_delimiter="__"`. Variables prefixed with
  `PG_DATABASE__...` do not populate `Settings.database`; missing effective
  credentials can fall through to other configuration/default behavior. One
  supplied variable also mixed a single underscore in `PG_DATABASE_HOST` with
  double underscores in the other names.
- **Decision:** Use `DATABASE__TYPE`, `DATABASE__HOST`, `DATABASE__PORT`,
  `DATABASE__DATABASE`, `DATABASE__USERNAME`, and `DATABASE__PASSWORD` in
  `rag_modules/config/.env`. The value must match the PostgreSQL service
  credential, but the secret itself is deliberately not recorded here.
- **Evidence:** `rag_modules/config/settings.py` defines
  `Settings.database: DatabaseSettings` and
  `env_nested_delimiter="__"`; commit `716ef3e` introduced multi-database
  support.
- **Status:** Configuration convention resolved; deployments still need their
  own secret injection and connectivity verification.
- **Lesson:** Environment variable names are derived from the settings object
  path, not from an arbitrary service prefix. Log the effective non-secret
  host/database/username at startup when troubleshooting, never the password.

### DB-001 — ORM table name did not match the existing schema

- **Source:** User supplied authoritative PostgreSQL DDL for `datasets`,
  `documents`, and `document_segments`; the original model used a
  knowledge-base table name.
- **Impact:** ORM queries targeted a table that did not exist in the real
  database and could not honor the existing foreign keys.
- **Root cause:** Product terminology (“knowledge base”) was used as a physical
  table name although the deployed schema uses Dify-style `datasets`.
- **Decision:** API/domain naming may remain knowledge-base oriented, but ORM
  `__tablename__` and foreign keys must match the physical tables exactly:
  `datasets`, `documents`, and `document_segments`.
- **Evidence:** Structure/multi-database commits `162fa1d`, `716ef3e`; schema
  foundation commits `e7e5aad` through `f134257`.
- **Status:** Resolved in the foundation schema implementation.
- **Lesson:** Separate product vocabulary from physical schema identifiers and
  test ORM metadata against the real database schema.

## 2. Product and infrastructure decisions that prevented later drift

### ARC-001 — Frontend flow differed from Dify

- **Source:** User report: create knowledge base first, then upload files;
  current page exposed different steps and Milvus configuration.
- **Impact:** Users saw backend infrastructure choices in the UI and could not
  follow the approved Dify-style workflow.
- **Decision:** Creation only captures name, description and permission, then
  navigates to documents. Configuration/preview comes after upload. Milvus is
  backend-only. Indexing offers high-quality and economy modes, with general or
  parent-child segmentation and real preview.
- **Evidence:** Design commit `4739752`, roadmap commit `7cef002`.
- **Status:** Design approved; frontend implementation remains in roadmap phase
  6.
- **Lesson:** UI should expose product choices, not storage/vector deployment
  details.

### ARC-002 — Approved file formats and indexing stack

- **Source:** User decisions during design review.
- **Decision:** Support `.txt`, `.md`, `.pdf`, `.docx`, `.xls`, `.xlsx`, and
  `.csv`; do not support OCR or `.doc` in this release. Use OpenAI-compatible
  Embedding, PostgreSQL metadata, backend Milvus, MinIO object storage, Celery,
  and RabbitMQ as broker. Redis is not part of the broker/result design.
- **Evidence:** Specs/plans in `4739752` and `7cef002`; RabbitMQ foundation
  `45b743b`; supported-type guard `ebcd12c`; MinIO implementation
  `bb0f90d` through `02a21e5`.
- **Status:** Foundation and parsing decisions implemented; Embedding/Milvus and
  Celery orchestration remain later roadmap phases.
- **Lesson:** Record rejected infrastructure choices explicitly; otherwise an
  old “Celery + Redis” assumption can silently return.

### ARC-003 — Source-change synchronization strategy

- **Source:** User proposed content hashes, polling/listening, and queue-driven
  near-real-time reindexing.
- **Decision:** Treat source changes as revisioned builds: detect add/update/delete,
  build new segments/vectors idempotently, activate safely, then clean old
  vectors. RabbitMQ/outbox and connector polling/listening are later phases;
  do not delete the active index before a replacement is ready.
- **Evidence:** Design `459e886`, plan `20e9416` and source-sync plans 8–11.
- **Status:** Design/plan; depends on complete indexing phases 1–5.
- **Lesson:** “Delete old vectors first” creates an availability gap. Build,
  validate and switch before asynchronous cleanup.

## 3. Parsing and preview implementation issues

### PARSE-001 — Text decoding could silently corrupt input

- **Source:** Task 2 independent review and later whole-phase review.
- **Symptoms:** UTF-8 bypassed text-quality checks; heuristic Han counting could
  reinterpret BOM-less UTF-16; later the ASCII-plus-round-trip GB18030 fallback
  silently decoded Shift-JIS, Big5 and CP1251 as Chinese-looking garbage.
- **Impact:** Corrupted text could be segmented, embedded and persisted without
  an explicit failure.
- **Failed/superseded decision:** Accept GB18030 whenever bytes contained ASCII
  structure and round-tripped exactly. This was recorded as “conservative” but
  was disproved: several legacy encodings satisfy the same conditions.
- **Final decision:** Accept UTF-8/UTF-8-SIG and structurally evidenced UTF-16.
  Ambiguous legacy input returns `TEXT_ENCODING_UNCERTAIN`; short valid GB18030
  may require conversion to UTF-8 or a future explicit charset.
- **Evidence:** Fixes `a3b6909`, `f94960b`, `ab8284f`, and `8fb64f4`; regression
  tests cover Shift-JIS, Big5, CP1251 and ambiguous GB18030.
- **Status:** Resolved and reviewed at `8fb64f4`.
- **Lesson:** Re-encoding equality proves reversibility, not the original
  encoding. Encoding detection must prefer false rejection over silent data
  mutation.

### PARSE-002 — Markdown front matter lost data or created unsafe graphs

- **Source:** Task 2 review and whole-phase review.
- **Symptoms:** Flat-only parsing discarded nested/list/scalar YAML; permissive
  `safe_load` still allowed anchors/aliases, cycles and amplification that later
  failed JSON serialization.
- **Impact:** Metadata could be silently lost or cause preview failure/resource
  exhaustion.
- **Decision:** Preserve safe structured JSON-compatible YAML, otherwise retain
  bounded raw text. Only exact column-zero `---` delimiters count. Reject aliases,
  anchors, duplicate keys, cycles, excessive nodes/depth/scalars and non-finite
  values.
- **Evidence:** `a3b6909`, `8fb64f4`.
- **Status:** Partially resolved. Deep alias-free YAML can still raise
  `RecursionError` during construction; tracked below as SAFE-005.
- **Lesson:** Post-construction graph validation is too late when construction
  itself can recurse or expand. Validate event structure first.

### PARSE-003 — DOCX malformed OOXML error boundary was too narrow, then too broad

- **Source:** Task 3 independent review.
- **Symptoms:** A valid ZIP with malformed internal OOXML leaked
  `XMLSyntaxError`; the first repair caught broad built-in exceptions around
  extraction and risked hiding programmer bugs.
- **Impact:** Unstable client errors initially; overbroad catch would later turn
  implementation defects into misleading “malformed document” responses.
- **Decision:** Separate library loading/parsing errors from extraction logic.
  Normalize malformed OOXML to `DOCX_MALFORMED`, but let programming errors in
  extraction surface during development/tests.
- **Evidence:** `cdc5e4e` followed by narrower fix `7ae1198`.
- **Status:** Resolved and independently reviewed.
- **Lesson:** Exception normalization belongs at dependency boundaries, not
  around an entire feature function.

### PARSE-004 — Spreadsheet normalization and budgets were inconsistent

- **Source:** Task 4 independent review.
- **Symptoms:** XLS/CSV nulls and XLS booleans were not normalized; ragged CSV
  blocks could use different headers; row budget reset per sheet; OpenPyXL
  declared dimensions treated style-only remote cells as real data limits.
- **Impact:** Incorrect searchable facts, inconsistent metadata and bypassable
  document-wide work limits.
- **Decision:** Normalize adapter-local scalar/null semantics; finalize one
  collision-safe header set for ragged rows; count non-empty logical rows across
  visible sheets; use physical/value evidence rather than worksheet dimensions.
- **Evidence:** `e9a2cd7` with tabular regression fixtures.
- **Status:** Functional issues resolved, but the original allowance for
  unlimited empty structural padding was superseded by later security review.
- **Lesson:** Logical content budgets and physical parser-work budgets are
  separate controls; both are required.

### SEG-001 — Preferred-boundary overlap could cause non-progress

- **Source:** Task 5 review; reviewer probes hung until interrupted.
- **Symptoms:** If a preferred boundary advanced no farther than overlap,
  `_split_ranges` could repeat or move backward. Example: `a。abcdefgh`, maximum
  8, overlap 2.
- **Impact:** Infinite loop and request/worker exhaustion.
- **Decision:** Use the preferred boundary only when its advance exceeds overlap;
  otherwise fall back to the hard maximum and assert strictly increasing start.
- **Evidence:** `175d4cb` and general/parent-child bounded regressions.
- **Status:** Resolved for progress, but extreme-overlap CPU cost remains a
  separate issue (SAFE-002).
- **Lesson:** Validate both output shape and algorithmic progress invariants.

### SEG-002 — Full-document fallback lost real delimiters

- **Source:** Task 5 review and final scoped re-review.
- **Symptoms:** Initial fallback dropped `\n\n` around prose/code/table groups.
  `175d4cb` restored them as delimiter-only sources. Later `8fb64f4` filtered
  blank segments and again reduced `aa\n\nx = 1\n\nbb` to `aax = 1bb` even under
  ordinary limits.
- **Impact:** Semantically separate source blocks become silently concatenated;
  preview and eventual stored content lose source fidelity.
- **Failed approach:** Represent delimiters as standalone source blocks, then
  globally filter blank blocks. Each individual rule looked correct, but their
  composition was not.
- **Decision:** Attach delimiters to adjacent nonblank bounded sources; omit only
  when pathological limits make any delimiter-bearing nonblank chunk impossible.
- **Evidence:** `175d4cb`, regression at `8fb64f4`, final review reproduction.
- **Status:** Open as SAFE-003; remediation design approved at `c567afc`.
- **Lesson:** A local invariant (“no blank segment”) must be tested together with
  end-to-end fidelity, not in isolation.

### PREVIEW-001 — Blocking MinIO read defeated request timeout

- **Source:** Task 6 independent review and wall-clock probe.
- **Symptoms:** Storage `stream.read()` ran with `abandon_on_cancel=False`; a
  50 ms deadline waited roughly 359 ms for a 350 ms blocking read and could wait
  forever if the transport never returned.
- **Impact:** Preview requests could exceed their deadline indefinitely and
  exhaust the AnyIO worker pool.
- **Decision:** Add bounded `ObjectStorage.get_bytes`; one worker owns
  `get_object -> read -> close -> release_conn`, while the request may abandon
  waiting safely.
- **Evidence:** `59be47f`; slow-storage API and adapter lifecycle tests.
- **Status:** Resolved and scoped re-review passed.
- **Lesson:** Cancellation cannot safely abandon a thread if resource ownership
  remains in the caller. Move the entire lifecycle into the abandoned unit.

### PREVIEW-002 — HTTP serialization was outside the request deadline

- **Source:** Task 6 review.
- **Symptoms:** Service timeout covered model construction, but FastAPI performed
  response validation and JSON rendering after the service scope exited.
- **Impact:** Large metadata could continue blocking the event loop beyond the
  advertised deadline.
- **Decision:** Start an outer deadline at handler entry and render a validated
  `JSONResponse` inside an abandonable worker within that scope. Keep the inner
  service timeout only as concurrent defense for direct callers.
- **Evidence:** `59be47f`; slow `model_dump` returns 504 near deadline while a
  dependency's own `TimeoutError` remains 503.
- **Status:** Resolved and scoped re-review passed.
- **Lesson:** A request-wide timeout must include serialization, not merely the
  domain service call.

### PREVIEW-003 — Error schema and production read-only proof diverged

- **Source:** Task 6 and whole-phase reviews.
- **Symptoms:** OpenAPI declared one 422 domain shape while Pydantic validation
  returned FastAPI `detail`; no-write tests replaced the whole service and could
  not prove production dependency composition.
- **Impact:** Generated clients saw an incorrect contract, and a future DI
  regression could introduce persistence/Embedding/Milvus calls unnoticed.
- **Decision:** A preview-local `APIRoute` produces stable
  `code/message/detail/request_id` errors and matching request ID header. A real
  production-composition test overrides only DB/storage, permits SELECT only and
  installs fail-fast external-client constructors.
- **Evidence:** `8fb64f4` and final scoped re-review.
- **Status:** Resolved and independently reviewed.
- **Lesson:** A unit test of a manually assembled service cannot prove the
  production dependency graph.

## 4. Whole-phase review findings and current remediation

### SAFE-001 — XLSX preflight can be bypassed and merge placeholders can expand

- **Source:** Final scoped re-review after `8fb64f4`.
- **Reproduction:** Rename a relationship target/member to `sheet1.XML`; the
  glob for lowercase `.xml` skips it while OpenPyXL still parses it. A single
  physical cell plus merge range `A1:Z100` can materialize roughly 2,600 `_cells`
  entries.
- **Impact:** Crafted XLSX can bypass physical-cell checks and allocate
  disproportionate CPU/memory before an HTTP timeout.
- **Root cause:** Preflight discovers parts by ZIP filename convention rather
  than workbook relationships and does not budget merged-range expansion.
- **Decision:** Resolve exact internal worksheet targets from relationships,
  reject unsafe package paths/relationships, preflight every resolved part, and
  enforce single/aggregate merge-area limits before OpenPyXL.
- **Status:** Open; Task 1 of the safety-hardening plan.

### SAFE-002 — Segment-count cap is not a CPU-work cap

- **Source:** Final scoped re-review plus controller complexity probe.
- **Reproduction:** With near-100% overlap, every chunk advances one character
  while `_boundary_end` copies/scans a very large window. Measured examples:
  1,000,000 characters with a 200,000 window took about 0.36 seconds;
  5,000,000 characters with a 1,000,000 window took about 1.79 seconds. The
  10,000-output cap can still permit billions of scanned characters.
- **Impact:** A timed-out preview leaves an abandoned worker consuming CPU for
  seconds or longer; the future Worker inherits the same risk.
- **Root cause:** Output allocation was bounded, but repeated boundary-search
  work and substring copies were not projected or accounted.
- **Decision:** Reject excessive projected iterations before splitting, charge
  actual boundary window sizes to a request-local scan budget, and use
  `str.rfind` bounds without copying the window.
- **Status:** Open; Task 3 of the safety-hardening plan.

### SAFE-003 — Ordinary delimiter fidelity regressed

- **Source:** Final scoped re-review.
- **Details:** See SEG-002. This remains open because the first final fix wave
  over-applied blank filtering beyond the approved pathological-limit exception.
- **Status:** Open; Task 3 of the safety-hardening plan.

### SAFE-004 — Formula warnings can fan out to nearly one million objects

- **Source:** Final scoped re-review.
- **Symptoms:** Each missing formula cache creates a warning; preview returns all
  warnings without a separate cap.
- **Impact:** A structurally valid workbook can consume large parser memory and
  produce an oversized JSON response even though chunks are truncated.
- **Decision:** Aggregate per sheet/code with count and five coordinate samples;
  bound warnings per document and across the preview response, folding overflow
  into `WARNINGS_TRUNCATED` without retaining omitted metadata.
- **Status:** Open; Task 2 of the safety-hardening plan.

### SAFE-005 — Deep alias-free YAML can fail before bounded normalization

- **Source:** Final scoped re-review.
- **Reproduction:** Roughly 1,500 nested flow-sequence levels can raise
  `RecursionError` inside `yaml.load`; the later depth check never runs.
- **Impact:** A small Markdown upload can cause an unhandled preview/parser
  failure and destabilize Worker execution.
- **Root cause:** Safety checks run after YAML object construction.
- **Decision:** Scan safe YAML events first with depth/event budgets and
  normalize `RecursionError` from scan, load and normalization to bounded raw
  metadata.
- **Status:** Open; Task 4 of the safety-hardening plan.

## 5. Process incidents

### PROCESS-001 — Final review found cross-property failures missed by task reviews

- **Symptoms:** Individual task reviews passed, yet whole-phase review found
  interactions between timeout/abandoned work, logical/physical budgets,
  nonblank/fidelity guarantees, serialization and metadata graph shape.
- **Root cause:** Task-scoped fixtures verified local behavior but did not always
  compose adjacent invariants under adversarial inputs.
- **Correction:** Final review now explicitly checks property pairs: bounded
  output plus bounded CPU; nonblank chunks plus faithful reconstruction; safe
  loader plus safe construction; parser warning behavior plus response caps;
  preflight plus dependency materialization.
- **Status:** Process correction active.

### PROCESS-002 — First final-fix agent stalled without producing changes

- **Symptoms:** The first final-fix worker ran for an extended period, ignored
  status requests and initially left the worktree unchanged. It was interrupted;
  later partial tests/encoding changes appeared, but no usable complete report.
- **Impact:** Lost wall-clock time and uncertainty about whether TDD had begun.
- **Correction:** A takeover agent was required to audit and preserve the
  existing partial diff, report after each group, and finish one coherent commit.
- **Evidence:** Takeover report records initial `15 failed, 73 passed`, then
  commit `8fb64f4`.
- **Lesson:** Require early observable RED evidence and periodic bounded status
  updates for broad repair tasks. Do not reset partial shared-worktree changes.

### PROCESS-003 — A large final fix still required a scoped adversarial review

- **Symptoms:** `8fb64f4` passed 240 tests but scoped review still found two
  Critical and three Important issues.
- **Root cause:** Green suites only prove represented cases. The fix changed
  several interacting boundaries and its new tests did not include relationship
  filename casing, merged-range materialization, scan complexity, construction-
  time recursion or warning cardinality.
- **Correction:** Create the separate approved safety-hardening design and plan,
  with one independently reviewed task per risk family and explicit adversarial
  fixtures.
- **Status:** In progress.
- **Lesson:** Test count is not a risk metric. Review must ask what input dimension
  remains unbounded and what downstream stage executes before validation.

## 6. Current execution map

| Work item | Plan task | Status | Expected commit |
|---|---:|---|---|
| Relationship-resolved XLSX and merge bounds | 1 | Design/plan | `fix: bound xlsx worksheet materialization` |
| Parser and preview warning caps | 2 | Design/plan | `fix: bound parser and preview warnings` |
| Segmentation CPU budget and delimiter fidelity | 3 | Design/plan | `fix: bound segmentation work and preserve delimiters` |
| YAML event preflight | 4 | Design/plan | `fix: preflight markdown front matter` |

After every task, append:

1. exact reproduction and expected failure;
2. root cause and why the previous control failed;
3. approaches rejected and why;
4. final implementation and compatibility cost;
5. RED/GREEN/full-regression commands and results;
6. commit SHA and independent-review verdict;
7. remaining or newly discovered risk.

## 7. Safety-hardening implementation records

### SAFE-001 Task 1 — Relationship-resolved XLSX preflight and merge bounds

- **Source and symptoms:** The Task 1 RED fixtures confirmed that filename-glob
  discovery skipped a relationship-resolved `xl/worksheets/sheet1.XML` member,
  allowing three physical cells past a limit of two. External relationships,
  authority/absolute URI targets, `..` traversal, missing members, duplicate
  relationship IDs, duplicate worksheet targets and a referenced non-worksheet
  relationship type all reached the fail-fast `load_workbook` hook instead of
  returning `XLSX_MALFORMED`. A sparse real cell plus `A1:Z100`, several legal
  ranges exceeding the aggregate limit, and malformed/reversed/out-of-sheet/
  missing merge references likewise reached OpenPyXL before rejection. Normal
  mode materialized `MergedCell` placeholders in private `_cells` mappings.
- **Root cause:** Preflight selected worksheet parts by a lowercase ZIP filename
  convention rather than resolving the workbook's sheet relationships. It
  counted `<c>` nodes but ignored `<mergeCell>` declarations, then extraction
  enumerated OpenPyXL's private mapping directly and used `worksheet.cell()` for
  cached values, which could create coordinates.
- **Plan ambiguity and ruling:** The written step said to reject absolute
  targets, but this repository and current OpenPyXL legitimately encode package
  targets as `/xl/worksheets/sheet1.xml`. The controller ruled that exactly one
  leading slash is a valid OPC package-root reference and is canonicalized to
  the exact case-sensitive member `xl/...`. Authority paths beginning `//`, URI
  schemes, drive paths, backslashes, query/fragment text, percent escapes, empty
  or dot segments and package traversal remain invalid. The RED absolute-target
  case therefore uses an authority/host target, not an OPC package-root target.
- **Rejected approaches:** Lowercasing member names was rejected because ZIP
  members and relationship resolution are case-sensitive. Retaining the glob
  and adding another suffix was rejected because relationships, not filenames,
  define worksheet identity. Letting OpenPyXL load before validation was
  rejected because merge placeholders allocate before the safety boundary.
  Enumerating cached cells with `.cell()` was rejected because lookup can mutate
  the mapping. Rejecting every leading slash was rejected because it breaks
  valid packages already produced and consumed by the project.
- **Final design:** Parse `xl/workbook.xml` and its relationship part with a
  hardened lxml parser, accept only the exact Transitional or Strict worksheet
  relationship URI, validate unique internal IDs/targets and resolve exact ZIP
  members in workbook order. Stream-preflight only those parts into immutable
  title/part/physical-coordinate records. Validate merge boundaries and XLSX
  coordinate maxima, enforce 100,000 single-range and 1,000,000 aggregate
  default areas, and avoid counting duplicate ranges twice. After two bounded
  OpenPyXL loads, verify sheet title/order and route all private mapping access
  through one adapter that walks sorted preflight coordinates with `.get()`,
  skips `MergedCell` placeholders and never calls `.cell()`.
- **TDD RED evidence:**
  `.venv/bin/python -m pytest tests/unit/config/test_settings.py
  tests/unit/parsing/test_tabular.py -k 'merged_cell or relationship or
  case_varied or external or traversal or missing_target or duplicate_target'
  -q` produced `12 failed, 31 deselected`: fields/validators were absent and
  every relationship breach reached the fail-fast loader. Then
  `.venv/bin/python -m pytest tests/unit/parsing/test_tabular.py -k 'merge or
  merged or physical_cell_adapter' -q` produced `7 failed, 1 passed, 34
  deselected`: merge breaches reached the loader and `_physical_cells` did not
  exist; the legal merged-workbook compatibility case already passed.
- **GREEN and regression evidence:** The same relationship command produced
  `12 passed, 31 deselected`; the same merge command produced `8 passed, 34
  deselected`. The focused Task 1 regression
  `.venv/bin/python -m pytest tests/unit/config/test_settings.py
  tests/unit/parsing/test_tabular.py -q` produced `51 passed` with one existing
  Starlette/httpx deprecation warning.
- **Fix commit to be created:** `fix: bound xlsx worksheet materialization`.
- **Residual risk/status:** The adapter deliberately depends on OpenPyXL's
  private `_cells` mapping contract, but that dependency is isolated and fails
  closed if its mapping/coordinate/cell-type assumptions change. ZIP payload
  size protection remains the upstream upload/decompression boundary. Task 2
  still owns bounded warning aggregation. Implementation is complete pending
  the full-suite verification, commit and independent review.

### SAFE-001 Fix Round 1 — Hyperlink materialization and hidden-sheet validation

- **Review symptoms:** Independent review reproduced an XLSX with only physical
  `A1` plus `<hyperlink ref="B2:C3">`: preflight retained only `(1, 1)`, but
  each OpenPyXL normal-mode load created real `Cell` entries for `B2`, `C2`,
  `B3` and `C3`. A near-sheet-sized hyperlink rectangle could therefore force
  large allocation before any configured limit. Review also found that the
  hidden-sheet `continue` ran before `_physical_cells`, so a hidden worksheet's
  formula/cached private mappings were never contract-validated. Finally, the
  relationship target rejection matrix did not explicitly cover URI schemes,
  drive paths, backslashes, query strings, fragments, percent escapes,
  single-dot segments, empty segments or the positive one-leading-slash case.
- **Root causes:** Worksheet preflight treated only `<c>` and `<mergeCell>` as
  possible OpenPyXL materializers; SpreadsheetML `<hyperlink ref>` ranges were
  omitted. Mapping validation was coupled to visible row emission instead of
  workbook-sheet validation. Target normalization implemented the intended
  rules, but tests covered only authority/traversal/missing/type cases.
- **Ruling and final design:** Keep `_WorksheetPreflight.physical_coordinates`
  strictly equal to actual XML `<c>` coordinates. Validate every hyperlink ref
  before either workbook load with the same range syntax and XLSX coordinate
  maxima used for merges. Conservatively charge physical `<c>` count plus each
  unique merge/hyperlink rectangle against the existing
  `max_physical_cells`; retain the merge-specific single/aggregate limits and
  errors. Exact duplicate materializing rectangles within a sheet count once;
  distinct overlaps and physical cells inside rectangles may be overcounted,
  which is deliberately fail-safe and avoids building a second potentially
  million-coordinate union. Over-budget hyperlinks reuse
  `TABLE_PHYSICAL_CELL_LIMIT_EXCEEDED`; malformed refs use `XLSX_MALFORMED`.
  Consume `_physical_cells` once for every formula/cached sheet pair before a
  hidden sheet is skipped. Both mappings may contain nonphysical extras only
  when each extra is a `MergedCell` or a real `Cell` with preserved hyperlink
  evidence; only actual preflight physical coordinates are yielded as table
  data.
- **Rejected approaches:** A new hyperlink-specific setting/error was rejected
  because the existing physical materialization budget and safe error fully
  express this bound. Adding hyperlink coordinates to `physical_coordinates`
  was rejected because it would violate the interface and risk treating
  relationship/location text as table data. Expanding rectangle unions during
  preflight was rejected because the safety check itself could allocate up to
  the configured million-cell bound. Rejecting every multi-cell hyperlink was
  unnecessary because current OpenPyXL preserves hyperlink evidence on each
  generated formula and cached `Cell`. Validating hidden mappings after the
  visibility branch was rejected because it preserves the bypass.
- **TDD RED and characterization evidence:** Before production changes,
  `.venv/bin/python -m pytest tests/unit/parsing/test_tabular.py -k 'hyperlink
  or hidden_sheet_mapping_contract or relationship_corruption or package_root'
  -q` produced `7 failed, 15 passed, 36 deselected`. The seven failures proved
  over-budget/malformed hyperlinks reached the fail-fast loader, a safe bounded
  hyperlink was rejected after loading, and a corrupt hidden mapping was
  skipped. All 15 target normalization/package-root cases passed against the
  old production code and are recorded as characterization rather than
  fabricated RED evidence.
- **GREEN and regression evidence:** The same focused command produced `22
  passed, 36 deselected`. The amended settings/tabular suite produced `67
  passed`. The full suite produced `277 passed, 1 skipped` with the existing
  Starlette/httpx deprecation warning. Compilation and `git diff --check`
  exited zero.
- **Fix commit to be created:** `fix: preflight xlsx hyperlink materialization`.
- **Residual risk/status:** Conservative overlap accounting may reject a
  workbook whose true materialized-coordinate union fits exactly under the
  budget; this is an intentional safety/complexity tradeoff. The accepted extra
  cells still rely on OpenPyXL preserving per-cell hyperlink evidence in both
  normal-mode workbooks, and fail closed if that private behavior changes. No
  new public setting or error code was introduced. Implementation is complete
  pending commit and re-review.

### SAFE-004 Task 2 — Bounded parser and preview warnings

- **Source, reproduction and symptoms:** The collector RED could not import
  `rag_modules.parsing.warnings` because no bounded warning abstraction existed.
  A generated visible XLSX sheet containing a header and 1,000 formulas without
  cached values produced 1,000 `FORMULA_CACHE_UNAVAILABLE` objects instead of
  one aggregate. A two-sheet workbook produced one warning for every formula
  plus an empty-sheet warning without a document cap. Preview preserved a
  parser `WARNINGS_TRUNCATED` object as an ordinary document warning and then
  appended every later parser/segmenter warning, returning seven warnings where
  the configured response limit was five.
- **Impact and root cause:** Parser memory and preview serialization grew with
  source-controlled formula/warning cardinality even though preview chunks were
  independently bounded. XLSX appended warnings inside the physical-cell loop,
  and preview eagerly extended a list. Neither layer reserved space for a safe
  truncation summary or folded a lower-layer omission count.
- **Ruling:** A limit includes its summary slot. Fewer than `N` ordinary
  warnings retain all; reaching `N` retains the first `N - 1` and summarizes
  the remainder. A preexisting summary folds only when `omitted_count` is a
  positive non-boolean integer. Missing, zero, negative, boolean and string
  counts are suppressed and deterministically contribute one omission, without
  retaining or reproducing their metadata. Visible-sheet formula aggregation
  occurs after its row generator is fully consumed and keeps only the first
  configured coordinates. Hidden sheets retain Task 1 mapping validation but
  are not extracted and do not emit formula warnings.
- **Rejected approaches:** Retaining the first `N` warnings and appending a
  summary was rejected because it violates the configured cap. Replacing the
  `N`th retained warning only after overflow was rejected because it retains an
  object/metadata that never belongs in the result. Nesting lower-layer
  summaries was rejected because it obscures the total and can preserve unsafe
  metadata. Adding formula text to aggregate metadata was rejected as needless
  source disclosure. Skipping hidden-sheet iteration was rejected because it
  reopens the private-mapping validation bypass fixed by Task 1. A broader lazy
  preview API was rejected as out of scope; the exact generic collector API
  means each overflow parser warning is briefly converted to a `PreviewWarning`,
  but the collector immediately releases it and retains no omitted object or
  metadata.
- **Final implementation and compatibility cost:** Added generic
  `BoundedWarningCollector[T]` over a small `WarningLike` protocol, plus positive
  parser/preview settings with defaults of 100 warnings and five formula
  samples. XLSX now accumulates count and ordered coordinate samples per visible
  sheet, emits one aggregate after row consumption, and routes hidden, empty,
  formula and summary warnings through the document collector. Preview routes
  document-qualified parser and segmenter warnings through the same collector
  and emits a neutral empty-document summary. Existing ordinary-warning order,
  source blocks, documents, chunks and `total_chunks` are unchanged; clients now
  receive bounded warnings and the aggregate formula metadata replaces the old
  per-cell `cell` shape.
- **TDD RED evidence:** `.venv/bin/python -m pytest
  tests/unit/parsing/test_warning_collector.py -q` failed during collection with
  `ModuleNotFoundError: rag_modules.parsing.warnings`. After adding only the
  collector/settings, `.venv/bin/python -m pytest
  tests/unit/parsing/test_tabular.py -k 'formula and warning' -q` produced `2
  failed, 58 deselected`: 1,000 warnings remained and the two-sheet result was
  unbounded. After XLSX GREEN, `.venv/bin/python -m pytest
  tests/unit/services/test_preview_service.py -k 'warning' -q` produced `1
  failed, 1 passed, 19 deselected`: the unbounded nested response did not match
  the five-slot folded result.
- **GREEN and regression evidence:** Collector/settings produced `29 passed`.
  Formula warning tests produced `3 passed, 58 deselected`, including hidden
  formula-sheet behavior. Preview warning tests produced `2 passed, 19
  deselected`. The final focused Task 2 regression produced `97 passed`. The
  fresh full suite produced `301 passed, 1 skipped`. Confirmed runs contained
  only the repository's existing Starlette/httpx deprecation warning.
- **Fix commit to be created:** `fix: bound parser and preview warnings`.
- **Residual risk/status:** Preview conversion of omitted warnings is transient
  allocation rather than retained growth; changing that would require an API
  outside this task. The collector assumes producers expose the documented
  `code` and mapping-shaped `metadata`. Implementation is complete pending final
  verification, commit and independent review.

### SAFE-005 Task 3 — Segmentation CPU budget and delimiter fidelity (executed 2026-09-04)

- **Source, reproduction and symptoms:** A 5,000,000-character source with a
  1,000,000-character maximum and 999,999-character overlap has a hard and
  minimum advance of one character, so the proven worst case is 4,000,001 split
  iterations. The previous emitted-record check reached `_boundary_end` before
  rejecting either the general or parent-child child path. Boundary scans had no
  separate request cap, allocated `text[start:limit]`, and could restart their
  effective allowance for every source. Separately, full-document fallback
  materialized standalone `\n\n` sources; the public nonblank filter then dropped
  them, reconstructing prose/code/prose as `aax = 1bb` instead of
  `aa\n\nx = 1\n\nbb` (and likewise around a table row).
- **Impact and root cause:** The output limit bounded retained records but not
  work performed before those records existed. Extreme overlap could therefore
  consume millions of iterations and repeated full-window scans. Boundary
  progress only required an advance greater than overlap, rather than a proven
  floor tied to the hard advance. Delimiter fidelity failed because synthetic
  delimiter-only parents conflicted with the correct rule that public search
  records must be nonblank; the fallback did not attach delimiter capacity to
  real adjacent text.
- **Ruling:** One request-local state owns the remaining emitted-record and
  boundary-scan budgets across all general sources, parents and children.
  Projection uses integers only: `hard_advance = maximum - overlap`,
  `minimum_advance = max(1, (hard_advance + 1) // 2)`, and either one range or
  `1 + ceil((length - maximum) / minimum_advance)`. Projection rejects but does
  not reserve records; only actual nonblank parent/child/general emission
  decrements the authoritative record count. Every `rfind` charges its exact
  `[start, limit)` search window before scanning. A preferred boundary must
  advance by at least `minimum_advance`; otherwise the hard maximum wins.
  Fallback keeps fitting code/table blocks standalone, tries the preceding
  non-atomic source without splitting, then the following source, and only then
  splits adjacent non-atomic text or the two newline characters as needed.
  Impossible delimiters are omitted and counted in one fidelity warning.
- **Rejected approaches:** Continuing to stop only on emitted records was
  rejected because output cardinality is not a CPU budget. Reserving projected
  records was rejected because later real emission would double-decrement the
  same capacity. Floating-point ceiling arithmetic was unnecessary and weaker
  for large lengths. Per-source scan counters, charging after `rfind`, retaining
  the window slice, and accepting every merely-positive advance were rejected
  because they fail the request-wide, fail-before-work, allocation, or progress
  contracts. Publishing delimiter-only parents, silently dropping all fallback
  delimiters, modifying a fitting atomic block, and splitting a full preceding
  prose block before trying a fitting following block were rejected for fidelity
  or atomicity reasons.
- **Final implementation and compatibility cost:** Added the default
  100,000,000-character scan budget and a private request-local work-budget
  object. `_split_ranges` now preflights its worst case, streams ranges, enforces
  the minimum advance, and uses indexed `str.rfind` without a window substring.
  Capacity-aware fallback streams bounded sources in source order, retains exact
  ordinary `\n\n` concatenation, can split a delimiter one newline to each
  eligible side at a two-character parent maximum, preserves fitting atomic
  blocks and traceable metadata, and emits `SEGMENT_DELIMITER_OMITTED` once with
  the aggregate count only when retention is impossible. The stable
  `SEGMENTATION_LIMIT_EXCEEDED` exception remains compatible with PreviewService's
  existing safe mapping.
- **TDD RED evidence:** `.venv/bin/python -m pytest
  tests/unit/segmentation/test_segmenters.py -k 'projected or boundary_scan or
  extreme_overlap' -q` produced `4 failed, 29 deselected`: both 5,000,000-character
  cases called the fail-fast boundary spy and both scan cases rejected the
  missing constructor parameter. `.venv/bin/python -m pytest
  tests/unit/segmentation/test_segmenters.py -k 'delimiter or
  fallback_preserves' -q` produced `5 failed, 28 deselected`: the too-early
  delimiter won, both atomic reconstructions lost newlines, and maximums one and
  two lacked the aggregate warning. Self-review added two narrower cases;
  `-k 'uses_following_prose or splits_delimiter_across'` produced `2 failed, 33
  deselected` before the precedence and split-delimiter refinement.
- **GREEN and regression evidence:** The prescribed work selector produced `4
  passed, 29 deselected`; the initial delimiter selector produced `5 passed, 28
  deselected`; and the two self-review cases produced `2 passed, 33 deselected`.
  `.venv/bin/python -m pytest tests/unit/segmentation
  tests/unit/services/test_preview_service.py -q` produced `56 passed`. The
  fresh full suite produced `310 passed, 1 skipped`. Runs contained only the
  repository's existing Starlette/httpx deprecation warning.
- **Fix commit to be created:** `fix: bound segmentation work and preserve
  delimiters`.
- **Residual risk/status:** Projection intentionally uses the proven worst case
  and can reject inputs whose actual preferred boundaries would require fewer
  records. Boundary prefix validation remains linear but performs no substring
  allocation and is coupled to a charged lookup window. Fallback keeps bounded
  per-block attachment state under the existing source-block cap; pathological
  whitespace already impossible to represent as nonblank public records remains
  outside the delimiter-specific warning. No public request or response schema
  changed. Implementation is complete pending final verification and commit.
