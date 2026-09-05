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

### SAFE-005 Review Fix Round 1 — Joint delimiter allocation (executed 2026-09-04)

- **Review symptom and root cause:** With three non-atomic blocks `AA`, `B`, and
  ` C` at parent maximum three, the greedy boundary allocator committed both
  characters of the first delimiter as a prefix on `B`. It then could not fit
  the second delimiter around the same one-character middle block or before the
  leading-space final block, so it emitted parents `AA`, `\n\nB`, and ` C` plus
  omission count one. That omission was false: the bounded nonblank allocation
  `AA\n`, `\nB\n`, `\n C` preserves both delimiters exactly. The root cause was
  making each boundary irrevocably without accounting for the prefix/suffix
  contention it creates on the next block.
- **Rejected approaches:** A fixture-specific redistribution rule was rejected
  because longer consecutive prose runs create the same contention. Retrying
  only the immediately failed boundary was rejected because correcting it may
  require revisiting an earlier attachment and can cascade. Exhaustive global
  search/backtracking was rejected because its state space grows exponentially.
  Treating a retainable delimiter as omitted, modifying fitting atomic blocks,
  weakening direct-fit precedence, or changing Task 3 CPU projection/scan logic
  were also rejected as violations or unrelated scope expansion.
- **Final bounded algorithm:** Delimiter ownership is a path problem with only
  three prefix states per block: zero, one, or two newline characters received
  from the preceding boundary. A reverse dynamic program considers the three
  suffix allocations plus one omission transition, validates the joint
  prefix/suffix capacity of the current block, and stores one backpointer per
  state. Its lexicographic objective minimizes omitted boundaries first, then
  incremental hard-chunk count, then the deterministic preceding/following
  direct-fit preference. Fitting atomic code/table blocks accept only the zero/
  zero state. Reconstruction assigns exactly two newline characters per retained
  boundary and increments the aggregate count exactly once per omission. Runtime
  is O(blocks × 3 × 4), memory is O(blocks × 3), and both remain bounded by the
  existing 100,000-source-block cap; split ranges remain streamed.
- **TDD RED evidence:** `.venv/bin/python -m pytest
  tests/unit/segmentation/test_segmenters.py -k
  'three_block_delimiter_contention' -q` on `3eee2ac` produced `1 failed, 35
  deselected`. The literal mismatch was `['AA', '\\n\\nB', ' C']` versus
  `['AA\\n', '\\nB\\n', '\\n C']`; the old result also carried the false
  `SEGMENT_DELIMITER_OMITTED` warning.
- **GREEN and regression evidence:** The isolated review regression produced `1
  passed, 35 deselected`. The legacy delimiter selector first produced `7
  passed, 29 deselected`. A compact max-two/three/four consecutive-prose matrix
  then produced `3 passed, 36 deselected`, proving exact reconstruction without
  duplicated or lost delimiters. The final covering delimiter selector produced
  `10 passed, 29 deselected`; segmentation plus PreviewService produced `60
  passed`; and the full suite produced `314 passed, 1 skipped` in 5.35 seconds.
  The final pre-commit gate repeated `60 passed`, the unchanged CPU-budget
  selector produced `4 passed, 35 deselected`, segmentation bytecode compilation
  exited zero, and `git diff --check` exited zero. Runs contained only the
  existing Starlette/httpx deprecation warning.
- **Fix commit to be created:** `fix: resolve delimiter allocation contention`.
- **Residual risk/status:** The secondary grouping objective estimates added
  hard chunks from lengths; it may choose a different valid grouping than a
  separator-aware optimum, but omission feasibility is determined independently
  and always takes priority. The DP retains three small backpointer entries per
  source block rather than constant memory, within the pre-existing source-block
  bound. Existing pathological source-internal whitespace caveats remain; no CPU
  budget, public API, warning schema, or fitting-atomic behavior changed.

### SAFE-006 Task 4 — YAML event preflight and stable recursion fallback (executed 2026-09-04)

- **Issue and reproduction:** Front matter containing 1,500 nested block
  sequences (`- ` repeated on one line) or 1,500 nested flow sequences contained
  no aliases or anchors and stayed below the 65,536-character raw limit, but both
  escaped `MarkdownParser.parse()` as `RecursionError` from PyYAML composition.
  A shallow 10,000-item sequence also produced more than 10,000 parser events
  while remaining below the raw-character limit and reached value construction.
- **Root cause and impact:** The existing YAML token scan rejected aliases and
  anchors, while `_normalize_front_matter` bounded depth and nodes only after
  `_BoundedSafeLoader` had recursively composed and constructed the complete
  value. That post-construction check was too late to protect the Python stack or
  limit parser-event work. The fallback caught YAML, type and value errors but
  omitted recursion failures, so malformed metadata could prevent otherwise
  ordinary Markdown body blocks from being returned.
- **Ruling and final design:** Stream `yaml.parse(raw, Loader=yaml.SafeLoader)`
  before `_BoundedSafeLoader` construction, `yaml.load`, and normalization. Count
  every event against the existing 10,000-node constant and collection starts
  against the existing depth constant of 20; reject aliases, an anchor on any
  event, negative depth, nonzero final depth and parser errors. Retain the token
  scan as defense in depth. Keep scanning, loading and normalization inside the
  existing front-matter-only fallback boundary, adding only `RecursionError` to
  its `TypeError`, `ValueError` and `yaml.YAMLError` tuple. Preserve body parsing
  outside that boundary, and let `MemoryError`, cancellation, other
  `BaseException` subclasses and unrelated programmer errors propagate.
- **Rejected approaches:** Relying on normalization alone was rejected because
  it requires constructing the dangerous graph first. Catching recursion only
  around `yaml.load` was rejected because event scanning and normalization can
  also recurse. Replacing the event stream with a second constructed node tree
  or overriding PyYAML parser internals was rejected as either too late or
  coupled to private state. Removing the token scan was rejected because the
  event pass is an additional preflight, not a replacement. Catching
  `Exception`, `BaseException`, or wrapping the entire Markdown parse was
  rejected because memory exhaustion, cancellation and body-parser defects are
  not recoverable malformed-metadata conditions.
- **TDD RED evidence:** Before production changes, `.venv/bin/python -m pytest
  tests/unit/parsing/test_text_markdown.py -k 'deep or recursion or event_budget'
  -q --tb=short` produced `6 failed, 30 deselected`: both real deep fixtures
  leaked recursion, the event flood reached a fail-fast `yaml.load` patch,
  `yaml.parse` was never called, and recursion from load and normalization
  escaped. The direct preflight/narrow-catch selector produced `5 failed, 36
  deselected`: the interface was absent for scalar-anchor, alias, negative-depth
  and unbalanced-depth cases, and a patched `yaml.parse` `MemoryError` was never
  reached.
- **GREEN and regression evidence:** The prescribed selector produced `6
  passed, 35 deselected`; the direct event-invariant and memory-exhaustion
  selector produced `5 passed, 36 deselected`. Markdown plus registry produced
  `48 passed`; the phase regression produced `245 passed`; and the final complete
  backend suite produced `327 passed, 1 skipped`. Runs contained only the
  repository's existing Starlette/httpx deprecation warning.
- **Fix commit to be created:** `fix: preflight markdown front matter`.
- **Residual risk/status:** Safe token scanning, safe event parsing and safe
  loading make up to three bounded passes over accepted front matter, trading a
  small deterministic CPU cost for defense in depth. Event counting is
  intentionally conservative because stream/document/scalar events share the
  10,000 budget with collection events. PyYAML may still raise an allowed
  recursion or parse exception before the configured threshold on a different
  runtime, but the same bounded-raw fallback handles it without exposing source
  text beyond 65,536 characters. No public parser or registry contract changed.

### SAFE-007 Whole-plan Fix W1 — PDF/XLS parser warning caps (executed 2026-09-04)

- **Whole-review finding and impact:** `XlsxParser` applied the configured
  per-document warning cap, but `PdfParser` and `XlsParser` accumulated ordinary
  warning lists. A PDF could retain one `PDF_EMPTY_PAGE` warning for every page
  before later valid text, up to the separate 500-page ceiling, and an XLS file
  could retain one warning for every hidden or data-empty sheet before a valid
  sheet. This violated the parser-level warning bound used by direct parser and
  future indexing-worker consumers.
- **Root cause and boundary ruling:** PDF and XLS predated the shared
  `BoundedWarningCollector` integration used by XLSX. Preview's collector was
  insufficient because it runs after parsing: each parser had already retained
  its complete warning list, and callers that consume parsers directly never
  pass through PreviewService. The cap therefore belongs at each producing
  parser, not only at an HTTP response boundary.
- **Rejected approach:** A broad `ParserRegistry` wrapper was rejected because
  it would hide noncompliance in individual parsers, risk changing every parser's
  ordering and error boundary, and still allow direct parser instances to bypass
  the cap. Copying the XLSX truncation factory into PDF and XLS was rejected to
  prevent warning-code, message, or metadata drift.
- **Implementation:** Added one shared `parser_warning_summary(omitted_count)`
  factory in `parsing/warnings.py`. PDF and XLS now add warnings to a
  request-local `BoundedWarningCollector` configured by
  `max_warnings_per_document`; XLSX reuses the same factory with byte-for-byte
  equivalent code, message, and `{"omitted_count": int}` metadata. Existing
  block extraction, warning order, malformed-input handling, resource cleanup,
  and no-extractable-text errors remain unchanged.
- **TDD RED evidence:** Before production edits, the focused PDF/XLS command
  produced `3 failed, 1 warning`: both the exact-limit and over-limit PDF cases
  returned raw `PDF_EMPTY_PAGE` entries instead of a summary, and XLS returned
  four raw hidden/empty-sheet warnings instead of its bounded prefix and summary.
  The later valid PDF paragraph and XLS table block assertions already passed.
- **GREEN and regression evidence:** The focused command produced `3 passed, 1
  warning`. Collector plus parsing plus PreviewService produced `159 passed, 1
  warning`. The single full-suite run produced `330 passed, 1 skipped, 1
  warning` in 5.78 seconds. The warning was the repository's existing
  Starlette/httpx deprecation warning.
- **Fix commit to be created:** `fix: enforce parser warning caps`.
- **Residual risk/status:** The collectors bound retained output and metadata,
  but parsers still construct one short-lived warning object per skipped page or
  sheet so omission counts remain exact; the independent PDF page, spreadsheet
  structure, and parser timeout/work limits bound that traversal. Other parsers
  were not wrapped broadly and retain their existing producer-specific warning
  strategies. No public parser model or warning schema changed.

### SAFE-008 Final integration review and fresh phase verification (executed 2026-09-04)

- **Review outcome:** Task-scoped review found and fixed two previously missed
  cross-layer XLSX conditions (hyperlink-range materialization and hidden-sheet
  mapping validation), one delimiter-capacity contention case, and the final
  cross-parser PDF/XLS warning-cap gap. Each Critical/Important finding received
  a separate fix commit and scoped re-review. The final W1 re-review found the
  PDF/XLS warning cap addressed with no new Critical or Important breakage.
- **Implementation commit chain after plan start `70c4e92`:** `e95360e` bounded
  relationship-resolved XLSX worksheet materialization; `60092d1` preflighted
  hyperlink materialization and validated hidden sheets; `68cba8e` bounded
  parser/preview warnings and aggregated formula warnings; `3eee2ac` bounded
  segmentation work and restored delimiters; `196f082` resolved joint delimiter
  allocation contention; `2cc45a4` preflighted Markdown front matter; and
  `c74d502` enforced PDF/XLS parser warning caps.
- **Fresh phase command:** `.venv/bin/python -m pytest
  tests/unit/config/test_settings.py tests/unit/parsing tests/unit/segmentation
  tests/unit/services/test_preview_service.py
  tests/api/test_indexing_preview_api.py tests/unit/object_storage
  tests/unit/repositories/test_document_repository.py -q` exited zero with `250
  passed, 1 warning in 5.26s`.
- **Fresh complete-suite command:** `.venv/bin/python -m pytest -q` exited zero
  with `330 passed, 1 skipped, 1 warning in 5.73s`. The one warning in both test
  runs is the pre-existing `StarletteDeprecationWarning` from FastAPI's
  `testclient.py` concerning the httpx compatibility package; no new parser,
  segmentation, preview, storage, repository, or API warning appeared.
- **Static and repository gates:** `.venv/bin/python -m compileall -q rag_modules
  main.py tests`, `git diff --check`, and `git status --short` each exited zero;
  the first two produced no output and the status was empty before this final
  documentation append. After this append, the documentation-only commit and a
  second diff/status check close the repository gate without changing runtime
  code.
- **Process problems and ruling:** The approved SDD workflow exposed two
  execution-process issues worth retaining for future work. First, a Task 4
  implementation turn remained in final self-review after all required gates
  were green; the controller interrupted it and resumed a narrowly scoped
  commit/report turn. Second, a new final-review agent could not be created
  because the session thread limit was reached. The controller therefore reused
  a read-only task reviewer in a new whole-branch review turn, then reused a
  different read-only reviewer as the fresh W1 implementer. No controller
  product-code edit, destructive action, merge, push, or external deployment was
  performed.
- **Final residual risks:** OpenPyXL `_cells` and hyperlink evidence remain
  isolated fail-closed private-API dependencies that must be revalidated on an
  OpenPyXL upgrade. XLSX rectangle-overlap accounting and segmentation
  projection deliberately favor safe over-rejection. Accepted YAML front matter
  receives three bounded passes. Warning producers construct bounded-lifetime
  transient objects to retain exact omission counts. These risks are documented,
  finite, and do not reopen the reviewed resource, disclosure, fidelity, or
  response-cardinality boundaries.

### EMB-001 Phase 4 Task 1 — OpenAI-compatible embedding boundary (executed 2026-09-04)

- **Problem and impact:** Phase 4 required a real high-quality indexing boundary
  that could call an OpenAI-compatible embedding service without trusting its
  response order or shape, leaking private inputs or backend details, retrying
  permanent failures, or returning mutable vectors. The repository had embedding
  configuration and public model discovery but no embedding client or typed
  result/error contract.
- **Root cause and design:** Added `EmbeddingBatch`, `EmbeddingError`, and
  `OpenAICompatibleEmbeddingClient`. The client resolves only enabled configured
  model IDs, validates all input before I/O, posts the configured backend model
  name and text batch to a normalized `/embeddings` endpoint, applies the model's
  per-request timeout, and reuses one `httpx.AsyncClient`. Each response is
  validated and reordered by its local indexes before immutable float tuples are
  appended in original global input order. An injected HTTP client remains owned
  by its caller; `aclose` and async context exit close only an internally created
  client.
- **Stable safe errors:** Unknown or disabled models use
  `EMBEDDING_MODEL_UNAVAILABLE` / `Embedding model is unavailable.`; empty,
  blank, non-string, or over-limit input uses `EMBEDDING_INPUT_INVALID` /
  `Embedding input is invalid.`; 401/403 uses `EMBEDDING_AUTH_FAILED` /
  `Embedding authentication failed.`; transport and HTTP failures use
  `EMBEDDING_REQUEST_FAILED` / `Embedding request failed.`; malformed count,
  indexes, vector shapes, JSON, types, or non-finite values use
  `EMBEDDING_RESPONSE_INVALID` / `Embedding response is invalid.`; and
  within-response, adaptive-child, or later-batch dimensional disagreements use
  `EMBEDDING_DIMENSION_MISMATCH` / `Embedding dimensions do not match.`. Only an
  exhausted timeout/network or 429/502/503/504 error is marked retryable. These
  errors never include model configuration exceptions, API keys, input text,
  response bodies, transport details, or vectors.
- **Boundary rulings and edge cases:** An empty text sequence is rejected before
  I/O because no truthful positive dimension can be returned. Empty or
  whitespace-only elements are rejected consistently. Response data must have
  the exact batch count and indexes exactly `0..n-1`; boolean indexes and vector
  values are rejected even though Python treats booleans as integers. Vectors
  must be non-empty JSON lists of finite JSON integers or floats. Extremely large
  integers that overflow float conversion are also sanitized as invalid
  responses. Dimension consistency is enforced within one response, across 413
  child requests, and across configured batches in one `embed` run.
- **Retry and adaptive-batch ruling:** An initial request plus at most
  `max_retries` transient retries uses bounded exponential delays and never sleeps
  after its final attempt. HTTP 413 is the sole adaptive-size signal: it is not
  retried at the failing size, splits the batch into smaller ordered halves, and
  terminates as a non-retryable request error at size one. HTTP 400 was not used
  as a split signal because no trusted machine-readable backend contract exists.
  Authentication, other permanent HTTP failures, invalid input, model lookup,
  and malformed responses never cause a split.
- **Rejected approaches:** Creating a new HTTP client per request was rejected
  because it forfeits pooling and makes lifecycle ownership unclear. Closing an
  injected client was rejected because that resource belongs to its caller.
  Returning backend order or mutable lists was rejected because both violate the
  consumer contract. Using exception strings, response text, or configuration
  errors in public failures was rejected as a disclosure risk. Retrying 413 at
  the same size or treating every 400 as evidence of an oversized batch was
  rejected as wasteful or unsafe. Accepting empty input with dimension zero was
  rejected because downstream collection setup requires a discovered positive
  dimension.
- **TDD RED evidence:** Before production files existed, `.venv/bin/python -m
  pytest tests/unit/embeddings/test_openai_compatible.py -q --tb=short` failed
  collection with `ModuleNotFoundError: rag_modules.embeddings`, proving the new
  protocol suite exercised an absent feature. The first implementation run had
  `37 passed, 4 failed`; all four failures identified test-fixture assumptions
  (`httpx.Request` has no `.json()` helper and httpx's response JSON encoder
  refuses NaN/Infinity), which were corrected by decoding request bytes and
  supplying raw non-finite JSON. A later valid-JSON huge-integer test failed with
  an escaping `OverflowError` while 11 sibling malformed-response cases passed;
  the narrow float-conversion guard made it GREEN. A separate dimension-code
  test failed with `EMBEDDING_RESPONSE_INVALID` instead of
  `EMBEDDING_DIMENSION_MISMATCH`; the classification was then corrected.
- **GREEN and verification evidence:** The complete embedding protocol suite
  finished with `43 passed, 1` pre-existing warning. Embedding plus configuration
  regression finished with `57 passed, 1` pre-existing warning. A fresh complete
  backend run finished with `373 passed, 1 skipped, 1` pre-existing warning in
  6.19 seconds. The warning remains FastAPI's existing Starlette/httpx test-client
  deprecation warning.
- **Commit:** Base `28b3cb0`; task commit subject `feat: add openai compatible
  embeddings`.
- **Residual risk/status:** Backoff is deliberately deterministic and capped at
  two seconds; it does not interpret provider-specific retry headers or add
  jitter. Recursive 413 subdivision is bounded by the configured maximum batch
  size of 512 and terminates at one item. The client does not impose a separate
  response-byte ceiling, so deployment transport/proxy limits remain responsible
  for bounding a maliciously large body. Callers that do not inject a shared
  client must close the wrapper or use its async context manager. No logging,
  PostgreSQL vector persistence, Milvus operation, indexing orchestration, or
  Celery behavior was added.

### EMB-002 Task 1 Fix Round 1 — closed-client lifecycle guard (executed 2026-09-04)

- **Review finding and impact:** Calling `embed` after closing an internally owned
  client reached `httpx.AsyncClient.post` and leaked httpx's raw `RuntimeError`.
  Closing an injected wrapper did not prevent later calls at all, and repeated
  owned closure delegated twice to the transport. This violated the typed safe
  failure boundary and left wrapper lifetime dependent on transport ownership.
- **Root cause:** `aclose()` delegated resource cleanup but did not record wrapper
  lifecycle state. `embed()` therefore had no state guard before model resolution,
  input validation, or I/O. Its deliberately narrow `httpx.RequestError` handler
  correctly did not catch an unrelated `RuntimeError`, exposing the missing state
  transition rather than an exception-classification problem.
- **Ruling and implementation:** Wrapper lifetime is now independent of transport
  ownership. `aclose()` first transitions any wrapper to closed, is idempotent,
  and physically closes an internally owned HTTP client exactly once. It never
  closes an injected HTTP client. After direct close or async-context exit,
  `embed()` checks the wrapper state before model lookup, input processing, and
  network use and raises non-retryable `EMBEDDING_CLIENT_CLOSED` with the stable
  message `Embedding client is closed.` No broad `RuntimeError` catch was added.
- **TDD RED evidence:** Before the production edit, the lifecycle selector had
  `3 failed, 1 passed`: direct owned reuse leaked `RuntimeError: Cannot send a
  request, as the client has been closed.`, an injected wrapper remained usable,
  and repeated owned close called the transport twice. The context-exit test was
  also run directly and failed because `EMBEDDING_MODEL_UNAVAILABLE` won before
  the required closed-client error, proving the guard-order defect.
- **GREEN and regression evidence:** The complete lifecycle selector produced `5
  passed, 41 deselected`. Embedding plus configuration produced `60 passed`; the
  fresh complete backend suite produced `376 passed, 1 skipped` in 6.04 seconds.
  Each run contained only the repository's existing Starlette/httpx deprecation
  warning.
- **Fix commit:** `fix: guard closed embedding clients` (separate from `23a725a`).
- **Rejected approaches and residual risk:** Broadly catching `RuntimeError` was
  rejected because it could conceal unrelated programming or transport defects.
  Treating injected-wrapper `aclose()` as a no-op was rejected because identical
  wrapper APIs would then have ownership-dependent reuse semantics. The guard
  defines sequential lifecycle behavior; callers must still coordinate a close
  racing with active `embed()` operations. No secret, input, backend response, or
  vector is included in the lifecycle failure.

### IDX-001 Phase 4 Task 2 — deterministic economy keywords (executed 2026-09-04)

- **Problem and impact:** Economy indexing required a local, retry-stable keyword
  path without an Embedding, Milvus, Celery, network, or logging dependency. The
  repository had `jieba` pinned but no indexing package or keyword contract, so
  later economy indexing could otherwise drift into nondeterministic token order,
  case-split English terms, lossy identifier handling, or unbounded caller output.
- **Design and rulings:** `KeywordExtractor.extract(text, limit=15)` rejects
  non-string text with `TypeError`, returns `[]` for blank text, rejects boolean
  or non-integer limits with `TypeError`, and rejects non-positive limits with
  `ValueError`. Contiguous Han spans are owned only by a private per-extractor
  `jieba.Tokenizer` with HMM disabled; non-Han Unicode word spans are owned only
  by a Unicode regex. This disjoint ownership prevents a mixed token such as
  `A001` from being counted once by each tokenizer. Explicit immutable English
  and Chinese stopword sets are filtered. Ordinary words lowercase; an ASCII
  alphanumeric identifier containing both a letter and a digit canonicalizes to
  uppercase while preserving internal `.`, `_`, or `-` (for example `a001` to
  `A001`). Rank is descending frequency plus a fixed identifier bonus, then the
  normalized term's lexical order; slicing guarantees output never exceeds the
  requested positive limit.
- **Resource and isolation ruling:** The input is scanned once, raw tokens are
  streamed, and the counter retains one entry per accepted distinct term, giving
  linear token-processing and distinct-term memory rather than materializing a
  second full token list. The implementation does not configure jieba globals,
  add dynamic dictionary entries, write a user dictionary, import external index
  clients, or perform I/O.
- **Rejected approaches:** Applying regex extraction to the whole input in
  addition to jieba was rejected because it double-counts mixed-script spans.
  Keeping jieba's default shared tokenizer or mutating its dictionary/log level
  was rejected to avoid global behavior changes. First-seen or tokenizer-native
  ordering was rejected because it can make equal-score results retry-dependent.
  Unicode locale-sensitive casing and preserving raw ID spelling were rejected
  because `a001` and `A001` must have one exact canonical representation.
- **TDD RED evidence:** Before production files existed, `.venv/bin/python -m
  pytest tests/unit/indexing/test_keywords.py -v` failed collection with
  `ModuleNotFoundError: rag_modules.indexing`. The system `python` lacked pytest,
  so the project `.venv` command is the recorded test runner. The first GREEN
  candidate had one assertion correction: `客户` and `订单` were tied and the
  approved lexical tie-break correctly ranked `客户` first; no production change
  was required for that test expectation.
- **GREEN and verification evidence:** Focused tests passed `17 passed`; keyword,
  embedding, and segmentation regression passed `102 passed`; and a fresh full
  suite passed `393 passed, 1 skipped` in 6.63 seconds. Each command emitted only
  the repository's existing FastAPI/Starlette httpx deprecation warning and the
  dependency's `pkg_resources` deprecation warning from jieba. `compileall` for
  `rag_modules/indexing` also exited successfully.
- **Commit:** `feat: extract economy index keywords` (base `0cbdbc3`).
- **Residual risk/status:** Chinese semantic token boundaries necessarily follow
  the pinned jieba dictionary; final extractor ranking is nevertheless owned and
  tested independently of jieba emission order. Very large inputs still require
  linear work and memory proportional to their distinct accepted terms, which is
  deliberate for this pure local extractor; upstream parsing and segmentation
  limits bound document-sized production calls. No external/vector/orchestration
  behavior was added.

### IDX-002 Task 2 Fix Round 1 — astral Han ownership and quiet initialization (executed 2026-09-04)

- **Review findings and impact:** The initial Han range covered only BMP code
  points. An astral Extension B character (`U+20000`) or CJK Compatibility
  Ideographs Supplement character (`U+2F800`) adjacent to `A001` was consumed by
  the non-Han word regex, producing `𠀀a001` or `丽a001` and losing the required
  canonical identifier. Separately, the private tokenizer's first Han extraction
  invoked jieba's lazy initializer, which emitted four debug records and stderr
  lines. Both defects violated the pure, deterministic local boundary.
- **Root causes and ruling:** The explicit class omitted astral Unified
  Ideographs Extensions B through H and the compatibility supplement. Inspection
  of the installed jieba 0.42.1 (within the declared `jieba>=0.42,<1` range)
  showed `Tokenizer.initialize()` accepts no instance logger and writes directly
  to `jieba.default_logger`; redirecting stderr alone would leave log records,
  while changing the shared logger's level, handlers, or propagation would leak
  behavior to concurrent consumers. The extractor now
  explicitly owns Unified Ideographs, Extensions A through J, and both
  compatibility ranges; it still does not use Unicode category/locale guesses
  that would absorb unrelated scripts. A private `_QuietTokenizer` mirrors only
  the observed initializer's dictionary-cache and lock behavior while omitting its
  initializer logging calls. It does not mutate global logger configuration and
  preserves shared dictionary-cache locking for concurrent initialization.
- **Rejected fixes:** Widening the regex to every Unicode letter/word character
  was rejected because it would merge unrelated scripts. Using a global
  `jieba.setLogLevel`, adding/removing logger handlers or filters, monkeypatching
  `jieba.default_logger`, and broad stdout/stderr redirection were rejected as
  global, observable, or concurrency-unsafe. Calling the shared default
  tokenizer was rejected because it would restore shared mutable state and its
  initializer chatter.
- **TDD RED evidence:** The new selected test run reported `3 failed, 17
  deselected`: both astral inputs returned a merged lowercase word instead of
  separate `A001`, and the capture test recorded four `jieba` initializer debug
  records (and matching stderr output). The expected values are literal external
  behavior, not derived from extractor helpers.
- **GREEN and verification evidence:** After the narrow repair, the selector
  reported `3 passed, 17 deselected`; the full keyword suite reported `20 passed`;
  `compileall` completed; and the final fresh full suite reported `397 passed, 1
  skipped` in 6.68 seconds. The only warnings were FastAPI/Starlette's existing
  httpx deprecation and jieba dependency's import-time `pkg_resources`
  deprecation; the new capture test verifies no runtime initializer output or
  jieba log record from the operation.
- **Fix commit:** `fix: handle unicode keywords quietly` (separate from
  `dcfd768`; recorded after commit).
- **Residual risk/status:** `_QuietTokenizer` deliberately tracks the
  initialization/cache behavior observed in jieba 0.42.1 but the project permits
  `jieba>=0.42,<1`; a public CJK initialization/tokenization regression and an
  explicit required-internal-attribute guard make an incompatible upgrade fail
  visibly, and any upgrade still requires a narrow compatibility re-review.
  Character ownership intentionally covers only named CJK Unified Ideograph and
  compatibility blocks, not radicals, strokes, or unrelated East Asian scripts.
  No Embedding, Milvus, Celery, network, application logging, or user-dictionary
  behavior was added.

### VEC-001 Phase 4 Task 3 — full Milvus vector-store protocol (executed 2026-09-04)

- **Problem and takeover:** The prior Task 3 implementer left only uncommitted
  configuration and Milvus tests, then became unresponsive. A replacement
  implementer audited those tests before production edits. During takeover, one
  rejected combined delete/add patch briefly left `milvus.py` deleted in the
  uncommitted worktree; the complete intended replacement was restored with
  `apply_patch` and immediately passed `py_compile` before any other edit or test.
- **Contract and configuration rulings:** `VectorEntity` is frozen, strict, and
  contains exactly the seven approved persistence fields. Milvus collections use
  an explicit non-dynamic schema with a caller-supplied positive dimension,
  VARCHAR(36) identifiers, nullable `parent_id`, INT64 `position`, and an HNSW
  COSINE index with `M=16` and `efConstruction=200`. Only COSINE is accepted.
  Batch size defaults to 500 and is bounded to 1..10000; consistency polling
  defaults to five attempts and 0.05 seconds, bounded to 2..100 attempts and
  0..5 seconds; connection timeout defaults to five seconds and is bounded to
  1..120 seconds.
- **Safety and lifecycle rulings:** Each store lazily creates and caches at most
  one client from its injected zero-argument factory. Disabled operational calls
  fail before client creation, while legacy disabled provisioning remains a
  compatible skip. Only `MilvusException` is translated at the SDK boundary;
  unrelated programming errors propagate. Public errors expose only fixed codes,
  retryability, and safe messages—never collection or entity identifiers, URI,
  credentials, backend details, or vectors. Known non-Milvus providers fail with
  `VECTOR_PROVIDER_NOT_IMPLEMENTED`; unknown providers fail with the sanitized
  `VECTOR_PROVIDER_INVALID` configuration error.
- **Operation rulings:** Existing collections are checked through the two
  approved PyMilvus 2.5 mapping/attribute description shapes, with missing or
  ambiguous schema/index data treated as a mismatch. Upsert validates the entire
  payload before I/O, re-discovers and caches the actual dimension after process
  restart, chunks writes, and requires an exact SDK write count. Empty upsert or
  ID deletion returns zero without creating a client. ID deletion validates all
  IDs before I/O, performs stable deduplication, and chunks requests. Document
  deletion accepts only trusted compact or hyphenated UUID text before building
  an equality filter. Drop is idempotent and invalidates cached schema state.
  Count flushes once and observes at most the configured attempt count; it returns
  only after two consecutive equal, valid, nonnegative row counts, sleeps only
  between observations, and otherwise raises retryable `VECTOR_COUNT_UNSTABLE`.
- **Rejected approaches:** Dynamic schemas, auto-generated IDs, content fields,
  non-COSINE metrics, and trusting configured rather than introspected dimensions
  were rejected because they weaken the persistence contract. Returning the last
  unstable count was rejected because it fabricates consistency. Broad exception
  catches were rejected because they hide programmer errors and cancellation-like
  failures. Interpolating arbitrary document text into a filter was rejected in
  favor of strict UUID validation. Eager or per-operation client construction was
  rejected because the store owns one reusable lazy client boundary.
- **TDD RED evidence:** With inherited tests present and production untouched,
  `.venv/bin/python -m pytest tests/unit/vector_stores/test_milvus_store.py -v`
  collected no tests and failed import with missing `VectorConsistencyError`.
  The expanded Milvus-plus-settings run failed at the same import boundary. The
  first implemented focused run collected 62 tests and reported 54 passed / 8
  failed; all failures exposed one test-oracle defect: the input identifier was
  literally `collection`, while the assertion prohibited that generic noun in
  safe messages such as `Vector collection schema does not match.` The tests now
  use a distinctive private identifier and verify that exact value is absent.
- **GREEN and verification evidence:** The corrected Milvus, settings, and
  factory set passed `62 passed` with one pre-existing Starlette/httpx warning.
  The single fresh full-suite run passed `445 passed, 1 skipped` in 6.83 seconds,
  with only the existing Starlette/httpx and jieba/pkg_resources deprecations.
  The restored adapter also passed direct `py_compile` before work resumed.
- **Scope and residual risk:** The 494-line adapter remains cohesive: its public
  methods are the required store protocol, and its private helpers centralize
  adapter-specific validation, batching, safe response parsing, and the two SDK
  introspection shapes. It was not split solely for line count. No embedding,
  keyword, PostgreSQL persistence, Task 4/5/6 orchestration, Celery, or live
  Milvus integration behavior was added. A real-service compatibility check
  remains Phase 4 Task 6; this task uses representative PyMilvus shapes only.

### VEC-002 Task 3 Fix Round 1 — validation boundaries (executed 2026-09-04)

- **Review findings and impact:** Four Important cases crossed their intended
  safe boundaries. `VectorEntity.position` accepted `2**63` for a Milvus INT64
  field. A decimal string longer than Python 3.11's integer-conversion digit
  limit escaped `_row_count` and `_positive_int` as raw `ValueError`. A valid
  nested index description hid contradictory direct `index_type`, `metric_type`,
  `M`, and `efConstruction`. Finally, explicit falsy factory inputs selected the
  configured provider and could construct it instead of returning
  `VECTOR_PROVIDER_INVALID`.
- **Root causes:** Position had only a nonnegative Pydantic bound. Both metadata
  parsers treated `isdecimal()` as sufficient proof that `int()` was safe and
  bounded. Index validation selected one representation instead of accounting
  for every present representation. Factory selection used `provider or
  default`, conflating an absent argument with invalid falsy values.
- **Rulings and implementation:** Position is constrained to the exact inclusive
  signed-INT64 range `0..9223372036854775807`; the existing adapter translation
  turns model rejection into `VectorValidationError` before any client, schema,
  or write access. String metadata conversion now checks a maximum of 19
  characters, ASCII decimal syntax, and signed-INT64 value before returning an
  integer. Count treats rejection as an invalid observation through all bounded
  attempts before `VectorConsistencyError`; schema and index validation classify
  it as `VectorSchemaMismatch`. If nested and direct index representations
  coexist, both normalize independently and both must match
  HNSW/COSINE/16/200; the two legitimate one-shape forms remain accepted. Only
  `provider is None` selects configuration; `""`, `0`, and `False` all fail with
  sanitized `VECTOR_PROVIDER_INVALID` before provider construction.
- **Rejected approaches:** Catching `ValueError` only at public operation
  boundaries was rejected because it would hide unrelated defects and still
  perform the oversized conversion attempt. Calling `int()` and then applying a
  numeric bound was rejected for the same resource reason. Trusting the nested
  index while ignoring direct fields, or permitting incomplete coexisting
  representations, was rejected as ambiguous. Retaining truthiness-based
  fallback or special-casing only the empty string was rejected because other
  explicit falsy non-string values would still broaden default dispatch.
- **TDD RED evidence:** After a clean pre-edit focused baseline of `62 passed`,
  the exact new selector reported `8 failed, 39 deselected`. The oversized
  position and conflicting mixed-index cases failed with `DID NOT RAISE`; the
  huge row-count, schema-dimension, and index-parameter cases each exposed the
  Python 3.11 `5000 digits` conversion `ValueError`; and `""`, `0`, and `False`
  each reached a patched constructor sentinel rather than the expected safe
  validation error.
- **GREEN and verification evidence:** The unchanged selector passed 8 tests
  with 39 deselected. The full Milvus/settings/factory set passed `70 passed` with
  the existing Starlette/httpx warning. Direct `py_compile` of the three
  production modules and the Milvus test module exited 0. The single fresh full
  suite passed `453 passed, 1 skipped` in 6.82 seconds with only the repository's
  existing Starlette/httpx and jieba/pkg_resources deprecations.
- **Fix commit:** `fix: harden milvus validation boundaries` (separate from
  `3d60c28`; recorded after commit).
- **Residual risk/status:** Textual metadata larger than signed INT64 is
  deliberately invalid even if Python could represent it; this matches the
  target field/count boundary and keeps conversion bounded. Live PyMilvus 2.5.14
  metadata and service behavior remain Task 6 scope. This fix round continues to
  cover only the two approved representative one-shape descriptions plus
  contradictory mixed metadata; it makes no live-service compatibility claim.

### VEC-003 Task 3 Fix Round 2 — validate before provider caching (executed 2026-09-04)

- **Review finding and impact:** Explicit unhashable provider values such as an
  empty list or dictionary raised raw `TypeError` instead of the sanitized
  non-retryable `VECTOR_PROVIDER_INVALID`. This left the public factory's safe
  configuration boundary dependent on whether an unsupported runtime value was
  hashable. Review also required cache identity to remain consistent for the
  configured default and the same explicit valid provider.
- **Root cause:** `lru_cache` decorated the public function, so its wrapper
  hashed positional arguments before the function body could normalize or
  validate them. The decorator also keyed an omitted call, an explicit `None`,
  and an explicit `"milvus"` separately even when all selected the same provider.
- **Ruling and implementation:** `get_vector_store` is now an uncached public
  validator/normalizer. Only `None` resolves the configured default; every other
  value must already be a supported string or it raises the fixed safe error.
  The normalized string is passed to a private `lru_cache` constructor, so only
  validated hashable values reach caching and equivalent configured/explicit
  providers share one instance. The facade retains its existing `cache_clear`
  test hook by delegating to the private cache. Known non-Milvus providers still
  raise `VECTOR_PROVIDER_NOT_IMPLEMENTED`; unknown strings and falsy hashable
  values still safe-fail before construction.
- **Rejected approaches:** Catching `TypeError` around the public call was
  rejected because decorator hashing occurs outside the body and a broad catch
  could conceal constructor defects. Converting lists/dictionaries to strings
  or special-casing only those two containers was rejected because it broadens
  or fragments provider validation. Removing caching was rejected because store
  identity is intentional. Keeping separate cache keys for `None` and its
  resolved provider was rejected because equivalent selection should reuse the
  same store.
- **TDD RED evidence:** The new selector reported `3 failed, 47 deselected`.
  Empty list and dictionary inputs each leaked `TypeError: unhashable type`; the
  identity test showed an explicit `"milvus"` call and the configured-default
  call returned distinct `MilvusVectorStore` objects.
- **GREEN and verification evidence:** The unchanged selector passed 3 tests
  with 47 deselected. The full Milvus/settings/factory scope passed `73 passed` with
  the existing Starlette/httpx warning. Direct `py_compile` of the factory and
  Milvus test module exited 0. The single fresh full suite passed `456 passed, 1
  skipped` in 6.81 seconds with only the existing Starlette/httpx and
  jieba/pkg_resources deprecations.
- **Fix commit:** `fix: validate vector providers before caching` (separate from
  `87623fc`; recorded after commit).
- **Residual risk/status:** The public function's dynamic runtime validation is
  intentionally broader than its static type annotation so malformed Python
  callers receive the stable error. The attached `cache_clear` attribute is a
  compatibility/testing hook backed directly by the private cache; construction
  identity is keyed only by normalized provider string. Live PyMilvus 2.5.14
  remains Task 6 scope.

### SEG-001 Phase 4 Task 4 — deterministic document-segment persistence (executed 2026-09-04)

- **Problem and approved boundary:** The indexing design required deterministic,
  retry-safe PostgreSQL `document_segments` staging that can later be activated
  only after a vector-store validation step. The repository had segment ORM
  columns but no stable identity/hash contract, no transaction-neutral staging
  boundary, no preview-parent-to-database-parent mapping, and no narrowly
  scoped activation/previous-version deletion operations.
- **Rulings and implementation:** `SegmentStagingCommand` carries dataset,
  document, target-index, job, technique, and segmentation snapshot values into
  `SegmentRepository.stage(command, segments)`. Content normalizes CRLF/CR to
  LF and Unicode NFC. Metadata recursively applies the same string treatment,
  accepts only finite JSON-compatible values, preserves list order, normalizes
  mapping keys, rejects duplicate normalized keys, and serializes using sorted,
  compact canonical JSON. SHA-256 hashes the canonical object containing both
  normalized content and metadata. A fixed application UUID namespace plus
  target index, document, deterministic persisted parent ID, position, and hash
  produces compact UUIDv5 retry IDs. Thus a configuration/index-version change
  changes identities even for identical text.
- **Parent, status, and transaction rulings:** Parent candidates are identified
  in a first in-memory pass; the subsequent pass resolves children from preview
  local IDs to those deterministic database IDs, so child-before-parent preview
  order remains valid. Missing/non-parent links, duplicate local IDs and derived
  IDs reject before any row is added. New rows stage as `indexing`; parents and
  all economy rows are `not_required` for embeddings while high-quality general
  and child rows wait. Exact retries return pre-existing immutable-equivalent
  records without creating duplicates or changing original job attribution;
  mismatched hash/content/metadata or immutable identity raises a safe conflict.
  All repository operations use `flush`, never `commit`; activation is limited
  to an explicit dataset/index/document's nondeleted staging records, and old
  rows are soft-deleted only for an explicit previous dataset index after later
  orchestration makes the replacement active. Timestamps come from aware UTC
  `utcnow`. Vectors are neither accepted nor stored or logged.
- **Encountered symptoms and root causes:** The initial test RED correctly
  failed module collection because `rag_modules.indexing.ids` did not exist. The
  first implementation test run then exposed a strict-plugin fixture authoring
  issue (`async fixture ... no plugin or hook`) caused by use of `@pytest.fixture`
  instead of `@pytest_asyncio.fixture`. It also exposed an existing ORM dialect
  mapping problem: actual SQLite table creation failed because its compiler does
  not support the PostgreSQL `ARRAY(Text)` keyword column. A SQLite-only JSON
  type variant restores real async-SQLite ORM testing while retaining PostgreSQL
  ARRAY and GIN behavior; no PostgreSQL schema column was changed. A later
  economy/parent-child design test intentionally REDed with `DID NOT RAISE`,
  revealing the missing command-level consistency check.
- **Rejected approaches:** Mock repository assertions and test-local SQL tables
  were rejected because the required test is real async SQLite behavior through
  the actual ORM mapping. Random IDs, source-metadata insertion order,
  content-only hashes, serial parent insertion dependency, mutation of exact
  retry rows, and automatic stage-time activation were rejected because they
  break reproducibility, linkage, audit history, or safe version switching.
- **TDD evidence to date:** `.venv/bin/python -m pytest
  tests/unit/indexing/test_segment_persistence.py -v` first failed collection
  with `ModuleNotFoundError`; after implementation it passed `12 passed, 2
  warnings in 0.08s`. The additive economy/parent-child test first produced `1
  failed, 11 passed`, then passed unchanged. Relevant db/repository/segmentation
  /keyword regressions passed `65 passed, 2 warnings in 0.94s`. The warnings are
  repository-existing Starlette/httpx and jieba/pkg_resources deprecations.
- **Final verification:** `py_compile` of every changed Python module and the
  persistence test exited zero; `git diff --check` exited zero; one fresh
  `.venv/bin/python -m pytest -v` run passed `468 passed, 1 skipped, 2 warnings
  in 6.96s`. The skip is the opt-in MinIO integration test and warnings are the
  existing Starlette/httpx and jieba/pkg_resources deprecations. Final review
  reconfirmed no vector payload/persistence/logging, no repository commit, and
  no Task 5 engine/activation invocation.
- **Final commit:** `feat: stage deterministic document segments` (its SHA is
  recorded in the Task 4 handoff after Git creates the commit).
- **Residual risk:** SQLite establishes actual asynchronous mapped persistence,
  but it cannot prove PostgreSQL locking or a cross-PostgreSQL/Milvus atomic
  transaction. A concurrent identical staging race still relies on the database
  primary key and Phase 5's transaction/retry orchestration to reload safely.

### SEG-002 Phase 4 Task 4 Fix Round 1 — race-safe batch segment staging (executed 2026-09-04)

- **Review finding and root cause:** `stage`'s sequential select then ORM
  add/flush flow allowed concurrent transactions to both observe missing IDs.
  The loser received a primary-key `IntegrityError`, which failed its entire
  caller-owned SQLAlchemy transaction. Thus an exact retry did not consistently
  return the existing row, while a conflicting collision did not become the
  public `SegmentPersistenceError` either.
- **Deterministic reproduction:** New real SQLite file tests use one async
  repository session and an independent committed connection. A cursor event
  inserts the competing row precisely when the repository begins its insert, so
  no wall-clock timing or mock assertions are involved. Before the fix, the
  focused suite reported `2 failed, 12 passed`; both race cases produced
  `sqlite3.IntegrityError: UNIQUE constraint failed: document_segments.id` at
  the unprotected flush. The test then requires staging a distinct document in
  the same outer session, which the old failure cannot safely do.
- **Ruling and fix:** Staging now uses one dialect-native batch `INSERT ... ON
  CONFLICT (id) DO NOTHING` for missing records (PostgreSQL production and
  SQLite real-behavior tests), then reloads every requested ID and checks the
  full immutable identity, content hash, normalized content, and metadata before
  it returns. An exact raced row therefore succeeds idempotently; an incompatible
  raced row safely fails only after reload; no expected collision exceptions or
  outer transaction rollback occur. This is batch-safe and avoids O(n) per-row
  savepoints. The method is still commit-free and uses the caller's transaction.
  Non-unique constraint/database/program failures are not caught or relabeled.
- **Timestamp correction:** Earlier tests asserted only the in-memory aware UTC
  value assigned by `utcnow`. They now refresh after persistence and assert the
  timestamp exists. SQLite does not round-trip timezone-awareness, so that
  limitation is documented rather than overclaimed; PostgreSQL remains mapped
  with `DateTime(timezone=True)` and receives aware UTC values.
- **Rejected approaches:** Broad `IntegrityError` catches would hide FKs/checks
  and unrelated failures. Per-row savepoints preserve a caller transaction but
  add avoidable O(n) write/rollback work and still require reload validation.
  Timing-dependent concurrent tasks were rejected for deterministic cursor-event
  injection through two actual database connections.
- **Final verification:** The unchanged focused persistence suite passed `14
  passed, 2 warnings in 0.10s`; relevant db/repository/segmentation/keyword
  regressions passed `65 passed, 2 warnings in 0.94s`; direct `py_compile` and
  `git diff --check` passed. One fresh complete `.venv/bin/python -m pytest -v`
  run passed `470 passed, 1 skipped, 2 warnings in 7.04s`; the skip is opt-in
  MinIO, and warnings remain the existing Starlette/httpx and jieba/pkg_resources
  notices. Final review verified no catch/commit/vector behavior in the race
  path and exact validation after the all-ID reload.
- **Final commit:** `fix: make segment staging race-safe` (the SHA is recorded
  in the Task 4 Fix Round 1 handoff after Git creates the commit).
- **Residual risk:** SQLite proves the ORM conflict result and continued
  transaction usability but not PostgreSQL deployment lock timeouts/isolation.
  PostgreSQL `ON CONFLICT` resolves a concurrent primary-key contender before
  the following reload under normal Read Committed semantics; production must
  retain ordinary database timeout/retry policy for disconnects or lock timeouts.

### IDX-001 Phase 4 Task 5 — single-document indexing engine (completed and approved 2026-09-04)

- **Approved boundary and ruling:** The single-document engine owns only
  `download -> parse -> split -> stage -> embed-or-keywords -> vector-upsert -> validate`.
  It checks cancellation between stages and batches, uses deterministic safe
  progress, and never activates rows or deletes previous versions. Existing
  high-quality targets come from the immutable command; building targets are
  resolved once from the first non-empty embedding dimension. Economy remains
  PostgreSQL-only. Parser, segmenter, keyword extraction, and Milvus calls are
  synchronous and must run off the event-loop thread; storage and Embedding stay
  asynchronous, with the storage context deterministically exited.
- **Vector-count ruling and cost:** Per-document validation is the exact sum of
  successful upsert return counts against this document's indexable row count.
  Calling `count(collection)` was rejected because its collection-wide value is
  invalid when documents append or execute in parallel. Independent whole-index
  cardinality validation and activation remain Phase 5 work; Task 5 proves only
  the provider's acknowledged counts for this document.
- **PostgreSQL vector ruling:** `DocumentSegmentRecord.vector` remains
  intentionally unmapped. Staging and engine APIs accept no PostgreSQL vector;
  embeddings exist only in `VectorEntity` batches sent to Milvus, leaving the
  legacy physical PostgreSQL column untouched/NULL. Adding pgvector or a shadow
  Python attribute merely to satisfy an illustrative assertion was rejected.
- **Initial tooling symptom/root cause:** The plan's literal `python -m pytest`
  selected `/Users/fitch/miniconda3/bin/python`, where pytest is not installed.
  This was an interpreter-selection error and not accepted as feature RED. The
  repository `.venv/bin/python` is the authoritative test interpreter.
- **Initial TDD RED:** Before production edits, `.venv/bin/python -m pytest
  tests/unit/indexing/test_document_engine.py -v` failed collection with
  `ModuleNotFoundError: No module named 'rag_modules.indexing.engine'`, exactly
  demonstrating the missing Task 5 engine.
- **Repository mutation defect and rejected approach:** The first atomicity
  probe passed only because SQLite happened to return the invalid row first; it
  was rejected as evidence. Deterministically ordering a valid row before an
  invalid row exposed partial in-memory mutation (`keywords=['valid']` and a
  completed child) before the safe error. Validation and mutation shared one
  loop. The fix validates the entire batch and materializes bounded keyword
  lists before changing any record, then flushes once. Relying on query row
  order or rolling back the caller's whole transaction was rejected.
- **Additional validation/lifecycle symptoms:** A leading-digit collection,
  non-string separator, and dimension 32,769 crossed the engine's validation
  boundary even though the Milvus adapter rejects them. The engine now matches
  the consumed adapter's exact collection grammar and `1..32768` dimension
  ceiling and validates the entire command before storage I/O. Plain
  `asyncio.to_thread` also allowed cancellation to close a source stream while
  its parser thread was still running. Sync work is now shielded and joined
  before resource release/cancellation propagation; worker defects remain
  visible rather than being relabeled.
- **TDD GREEN and final verification:** The engine suite grew from the initial
  valid missing-module RED to 38 collected cases. Repository API RED was `2
  failed, 1 passed`, then `3 passed`; deterministic atomicity RED was `2
  failed`, then `2 passed`; validation REDs were `3 failed` and later `2
  failed`, then passed unchanged; lifecycle RED was `1 failed`, then passed.
  The final required engine/Embedding/vector/keyword selector passed `155
  passed`; repository/parser/segmentation regressions passed `197 passed`;
  direct `py_compile` and `git diff --check` exited zero; one fresh full suite
  passed `513 passed, 1 skipped, 2 warnings in 7.10s`. The skip is opt-in live
  MinIO and warnings are existing deprecations.
- **Residual risk:** External boundaries are deterministic unit fakes here;
  live multi-service integration remains later scope. Per-upsert acknowledgement
  is not independent collection cardinality proof. Phase 5 still owns caller
  transaction disposition after partial external success, whole-index
  validation, compensation, activation, and previous-version deletion.
- **Final commit:** `feat: index one document into postgres and milvus` (SHA is
  recorded in the Task 5 handoff after Git creates the commit).

### IDX-002 Task 5 Fix Round 1 — cancellation and document-sized vector retention (completed and approved 2026-09-04)

- **Review findings:** Cancellation during a synchronous successful Milvus
  upsert propagated before acknowledgment validation and PostgreSQL status
  flush, leaving the written batch `waiting`. A second cancellation interrupted
  the cleanup join and could close a stream beneath its parser. Separately, the
  engine retained every batch's vectors until all document Embeddings finished,
  allowing multi-gigabyte retention at the approved segment/dimension ceilings.
  Economy mutation had no check after its final progress callback; repository
  mutation APIs allowed cross-technique state corruption; syntactically valid
  arbitrary warning codes could carry secret-looking data.
- **Root causes:** One helper conflated ordinary non-abandonable sync execution
  with a vector operation critical section. Its cleanup await was unshielded.
  Embedding and vector writes were implemented as two document-sized materialized
  phases. Repository validation checked segment type but not technique-derived
  embedding state. Warning filtering checked grammar rather than a closed
  producer set. Economy checked cancellation before extraction and after the
  eventual mutation, not at the progress-to-mutation boundary.
- **Rulings/rejected approaches:** Process exactly one vector batch at a time
  and defer cancellation through upsert acknowledgment plus matching status
  flush. A task-completion-bounded repeated-shield loop is permitted; suppressing
  cancellation, abandoning a live thread, or adding an independent unbounded
  wait/retry loop is rejected. Worker/ack/status defects win over pending
  cancellation so programmer/dependency failures remain visible. Repository
  technique comes from the established `embedding_status` invariant rather
  than duplicating technique parameters. Unknown warning codes map to one fixed
  generic code; syntax filtering is rejected. Collection count, activation,
  deletion, logging, and orchestration remain out of scope.
- **TDD RED:** The focused engine selector reported `5 failed, 37 deselected`,
  reproducing every engine finding. The real SQLite repository selector reported
  `3 failed, 19 deselected`, reproducing both cross-technique writes and
  non-idempotent completed timestamp mutation.
- **Additional cancellation-boundary RED:** A final state-aware progress test
  proved streaming batch two had no cancellation check between its Embedding
  result and vector write (`DID NOT RAISE CancelledError`). The explicit check
  now occurs after every validated/resolved Embedding batch, not only the first
  stage update; the unchanged selector passed.
- **GREEN and final verification:** The six-finding engine selector passed `6
  passed`; the real SQLite repository selector passed `3 passed`; worker-error
  precedence plus repeated cancellation passed `3 passed`; full Task 5 files
  passed `64 passed`. The final focused Task 5/Embedding/vector/keyword scope
  passed `161 passed`; repository/parser/segmentation regressions passed `200
  passed`; direct `py_compile` and `git diff --check` exited zero. One fresh
  full suite passed `522 passed, 1 skipped, 2 warnings in 7.31s`; the skip and
  warnings remain the existing opt-in MinIO/deprecation set.
- **Residual risk:** Peak vectors are bounded to one configured batch, not zero;
  the maximum batch of 1,024 can still be sizable for extreme dimensions.
  Robust join cannot terminate a dependency call that itself never returns,
  though it cannot outlive a completed task and never abandons the live thread.
  Worker/ack/status defects deliberately win over a pending cancellation.
  High-quality progress announces the approved stages monotonically while
  later Embedding/upsert pairs stream internally. Phase 5 still owns process
  deadlines, transaction/failure mapping, compensation, and activation.
- **Fix commit:** `fix: bound indexing batches and cancellation` (SHA is recorded
  in the fix-round handoff after Git creates the commit).

### IDX-003 Phase 4 Task 6 — live Milvus 2.5.14 compatibility verification (2026-09-04)

- **Scope and environment:** Task 6 exercised the existing Compose
  `milvusdb/milvus:v2.5.14` standalone service through the project virtualenv
  (`Python 3.11.15`, resolved `pymilvus 3.0.1`). The shell-default Python was
  3.14.6 and had no PyMilvus installed; that was an interpreter-selection
  condition, not an adapter failure. The declared dependency remains
  `pymilvus>=2.5.0`; no pin or downgrade was made.
- **Compose preflight and rejected cleanup:** Compose interpolates the complete
  file before selecting services. Without a worktree `.env`,
  `docker compose config --services` initially required unrelated RabbitMQ,
  Neo4j, and PostgreSQL variables. Process-only benign integration placeholders
  for those required interpolations plus dedicated MinIO credentials allowed
  config expansion; no `.env` or credential values were persisted. The plan was
  to start only `etcd`, `minio`, and `standalone`. Direct sandbox Docker access
  was denied at `/Users/fitch/.docker/run/docker.sock`, so elevated Docker
  access was requested rather than bypassed. Startup then found fixed container
  name `milvus-etcd` already owned by the primary-worktree Compose project.
  Removing, renaming, stopping, pruning, `down -v`, or touching shared volumes
  was rejected. Targeted inspection instead established that the existing
  `graph-rag` containers were `milvus-etcd` (v3.5.18, Up/healthy),
  `milvus-standalone` (v2.5.14, Up/healthy), and `milvus-minio`
  (RELEASE.2024-05-28T17-19-04Z, Up/unhealthy). An elevated localhost health
  probe returned `OK` from standalone. Sandbox-localhost isolation initially
  made both port 19530 and health port 9091 unreachable; elevated test execution
  was required to reach the already-running service. The application MinIO
  adapter was neither exercised nor changed by this test, but Milvus standalone
  remains configured against that pre-existing MinIO backend; its unhealthy
  state is therefore a residual environment concern even though the vector path
  passed.
- **Isolated test and skip evidence:** Added an opt-in `RUN_INTEGRATION=1`
  fixture with explicit safe localhost/default-database configuration and a
  unique `test_<uuid>` collection per test. With integration disabled,
  `tests/integration/test_milvus_index.py -v` reported `2 skipped, 1 warning`.
  The main test creates/revalidates the exact schema and HNSW/COSINE index,
  repeats a stable-ID upsert, checks count one, deletes it, checks count zero,
  and drops only its own collection in `finally`. A second test deliberately
  raises an assertion inside its body and proves the same owned-collection
  `finally` cleanup path. No broad fixture enumerates or drops collections.
- **Schema compatibility RED/root cause/fix:** The first elevated live run
  reached Milvus but failed on the second `ensure_collection`: the adapter
  rejected the schema it had just created. A fresh owned diagnostic collection
  (dropped in `finally`, `cleanup_exists=False`) showed the real server/SDK
  response omits `nullable` for every non-nullable field while retaining
  `nullable=True` for `parent_id`. The prior strict comparison required an
  explicit `False`. A focused unit regression first failed (`1 failed,
  50 deselected`), then passed after normalizing only a missing nullable value
  to the Milvus non-nullable default. Explicit invalid values and missing
  nullable metadata for `parent_id` still fail schema validation. Pinning or
  downgrading PyMilvus and weakening all schema checks were rejected.
- **Logical-count compatibility RED/root cause/fix:** After the schema fix,
  repeated same-primary-key upserts produced physical
  `get_collection_stats().row_count == 2`, while a same-ID query returned one
  logical row. A second owned diagnostic confirmed
  `query(output_fields=['count(*)'])` returns logical count one even after the
  duplicate physical/tombstone statistic; it and a return-wrapper probe each
  ended with `cleanup_exists=False`. PyMilvus 3.0.1 returns a
  `HybridExtraList` that is iterable/indexable as one `{'count(*)': N}` mapping.
  The focused logical-count TDD regression first failed (`1 failed,
  50 deselected`) because the old stats path returned zero in the fake, then
  passed after the adapter kept its existing flush/two-equal-observations
  polling but read and validates the one-row logical aggregate query instead.
  Retaining physical `row_count`, treating it as an upsert-idempotency oracle,
  or adding arbitrary sleeps were rejected: physical records include replaced
  versions and the adapter already uses bounded condition polling.
- **GREEN/live and cleanup evidence:** The focused schema/count/mismatch suite
  passed `14 passed, 37 deselected, 1 warning`. Controller-run fresh live
  verification of the approved command reported schema subset `8 passed,
  43 deselected` and `RUN_INTEGRATION=1` integration `2 passed, 1 warning in
  13.96s`. The successful test exercises its own `finally` drop; the separate
  failure-path test passed. Each diagnostic used a generated `test_<uuid>` name
  and separately proved only that owned name absent afterward; this does not
  claim an inventory of arbitrary shared collections. Output and durable notes
  intentionally contain no tokens, passwords, or raw backend exception
  payloads.
- **Residual risks:** Live coverage is one local existing service stack, not a
  clean Compose lifecycle test, because fixed shared names prevented a second
  stack and scope prohibited destructive resolution. The MinIO healthcheck is
  unhealthy but outside this vector-only test. Future PyMilvus response shapes
  can change, though the observed omitted-default and aggregate wrapper shapes
  now have focused regressions. Collection-wide logical count is appropriate to
  this adapter API; Task 5 continues to use per-document acknowledged upsert
  counts for concurrent document validation.

### IDX-004 Task 6 Fix Round 1 — sanitize cleanup verification failures (2026-09-04)

- **Review finding/root cause:** The integration-only `_collection_exists()`
  evidence helper reached the private raw Milvus client. If that check raised a
  `MilvusException`, pytest could display its backend detail even though the
  application adapter sanitizes its public boundary.
- **TDD RED:** A deterministic no-live fake client raised MilvusException with
  a distinctive backend-only detail. The focused test failed as the raw SDK
  exception (`1 failed, 2 deselected`), proving the disclosure path without
  creating or changing any collection.
- **Fix and rejection:** The helper now catches only `MilvusException` and
  raises one fixed generic `AssertionError` with `from None`. The no-chaining
  test verifies the fixed text, absence of the distinctive detail, no cause, and
  suppressed context. Catching arbitrary exceptions was rejected so programmer
  errors remain visible.
- **GREEN/verification:** Focused sanitizer GREEN was `1 passed, 2 deselected`.
  With `RUN_INTEGRATION` absent the module now reports `1 passed, 2 skipped`;
  the pure sanitizer runs locally and only the two live tests skip. Elevated
  live verification against Milvus 2.5.14 passed `3 passed, 1 warning in
  14.19s`. No Compose startup, container mutation, broad cleanup, or live
  collection outside a generated test-owned name occurred.

### IDX-005 Phase 4 Final Fix Wave — persistence, retry, and physical-unit contracts (2026-09-04)

- **A — storage disclosure symptom and root cause:** `SegmentRepository`
  allowed raw SQLAlchemy execute/flush failures to escape. Those exceptions can
  retain rendered statements, bound `content`/`source_metadata`, backend text,
  parameters, causes, and contexts, which could then reach job errors or logs.
  The repository now catches only `SQLAlchemyError` at each database operation
  and raises a fixed `SegmentStorageError` outside the handler with `from None`.
  Operational, interface, disconnect, timeout, and invalidated-connection
  failures are conservatively retryable; data, integrity, and other SQLAlchemy
  failures are not. Validation errors, cancellation, and programmer errors keep
  their original behavior. Runtime-generated private values prove the public
  exception, `str`, `repr`, chain, and formatted traceback are content-free,
  without persisting a test value here. All runtime, legacy, and Alembic async
  engine factories now set `hide_parameters=True` as a second boundary.
- **A — rejected approaches and cost:** Copying a backend message or original
  exception into the public type was rejected because either can retain bound
  data. Catching `Exception`, or wrapping validation and statement-building
  code, was rejected because it would misclassify cancellation and defects.
  Parameter hiding alone was rejected because exception objects still expose
  parameter structures. The fixed boundary intentionally sacrifices backend
  detail at the public edge; private operational diagnostics must come from a
  separately controlled observability path.
- **B — bind-ceiling symptom, root cause, and ruling:** A valid 10,000-segment
  preview produced one PostgreSQL multi-values insert with roughly fourteen
  binds per row and one all-ID `IN` reload. Both could cross PostgreSQL's bind
  ceiling. Staging now fully prevalidates first, then emits dialect-native
  `ON CONFLICT (id) DO NOTHING` inserts in explicit groups of at most 500 and
  reloads all candidates in groups of at most 500, combines them by ID, and
  performs the same immutable recheck in original preview order. Exact mutation
  reads use the same bound. The transaction remains caller-owned, and the
  existing deterministic SQLite race semantics remain unchanged.
- **B — rejected approaches and cost:** Lowering the public 10,000-segment
  ceiling or relying on an ORM/driver to split statements was rejected because
  neither preserves the approved contract. A single expanded `IN` clause and
  per-row queries were rejected. The conservative constant avoids dependence on
  current column-count arithmetic but adds bounded database round trips: a full
  10,000-row stage uses twenty inserts, twenty existence-read statements, and
  twenty post-insert reload statements.
- **C — lifecycle symptom, root cause, and ruling:** Exact retry comparison
  previously ignored `status`, deletion, and embedding state, so terminal,
  activated, deleted, or cross-technique rows could be treated as mutable
  staging. Repository equality now requires a non-deleted `indexing` row;
  high-quality general/child rows allow `waiting` or `completed`, while parents
  and economy rows require `not_required`. The engine independently validates
  every returned staged record before resolver, Embedding, or Milvus access and
  emits one fixed non-retryable state error on violation. A partial
  high-quality retry skips already-completed records, processes only waiting
  records, and does not change existing attribution.
- **C — result ruling, rejected approaches, and cost:** On successful return,
  `vector_count` is total ready indexable rows for this document: completed rows
  accepted from staging plus newly acknowledged writes. It is not writes made
  during this invocation. Re-embedding/upserting completed rows and requiring a
  new acknowledgement for them were rejected because they defeat idempotent
  partial retry. Calling collection count was rejected because concurrent
  documents make it the wrong per-document invariant. The engine therefore
  trusts the completed staging marker until Phase 5 performs collection-wide
  reconciliation; that trust is an explicit recovery boundary.
- **D — physical-batch symptom, root cause, and ruling:** The engine accepted a
  1,024 batch while the selected Embedding model normally permits at most 512
  and Milvus writes default to 500, so one apparent engine unit could silently
  become several HTTP and vector operations with misaligned cancellation and
  status acknowledgements. `IndexDocumentCommand` now snapshots and validates
  separate `embedding_batch_size` and `vector_batch_size` values. The effective
  streaming unit is their minimum with the 1,024 engine ceiling. Each normal
  high-quality unit is embedded, dimension-validated/resolved when first, sent
  as one bounded upsert, and marked completed only after its exact
  acknowledgement; cancellation is checked before the next unit.
- **D — rejected approaches and cost:** Reaching into adapter private settings,
  treating adapter-side defensive chunking as the engine contract, or resolving
  dimension after multiple batches were rejected. The new required immutable
  command field makes Phase 5 snapshot vector configuration explicitly. The
  Embedding client's bounded adaptive split after an HTTP size rejection remains
  an internal exception to one-request normal behavior, and Milvus keeps its
  defensive split for direct callers.
- **E — keyword symptom, root cause, and ruling:** The extractor could emit a
  256–1,024-character word/identifier that the repository correctly rejected at
  its 255-character storage boundary. A dependency-free shared
  `MAX_KEYWORD_LENGTH = 255` now controls both sides. Extraction omits an
  overlong normalized token; persistence still validates defensively. Truncation
  was rejected because different source terms could collide. A real SQLite
  repository/engine economy regression proves an overlong-only token completes,
  stores only valid bounded keywords (possibly none), and makes no external
  vector calls.
- **F1–F2 — characterization findings:** Stable segment ID tests now vary
  dataset index, document, parent, position, and content hash independently,
  parse the result as UUID version 5, and require 32 lowercase hexadecimal
  characters. PostgreSQL compilation proves `keywords` is `ARRAY(Text)` with
  the intended GIN index while SQLite remains JSON. Both new tests passed during
  RED, so production already honored these contracts and no artificial code
  change was made.
- **F3–F6 — small review findings:** The indexing package initializer eagerly
  imported the engine and therefore PyMilvus, breaking dependency-light economy
  imports. It now exposes the same API through lazy module attributes, with a
  subprocess regression that blocks PyMilvus and verifies engine/vector modules
  stay unloaded. Milvus logical-count parsing now rejects a direct Python int
  above signed INT64 while accepting the exact maximum, matching the existing
  decimal-string bound. Historical Task 2 status and Task 5 retrospective
  headings now state their completed/approved state. Obsolete collection-stats
  fields and methods were removed from the unit `RecordingMilvusClient`; no
  production stats API was reintroduced.
- **F3–F6 — rejected approaches and cost:** Moving PyMilvus imports into economy
  code, swallowing blocked imports, or narrowing the public package API were
  rejected; lazy exports preserve compatibility at the cost of first-access
  resolution. Converting arbitrary direct integers through the existing string
  parser was rejected in favor of an explicit signed-INT64 check. The already
  conforming F1/F2 behavior was not rewritten, and the fake-only stats cleanup
  deliberately changes no adapter behavior.
- **TDD RED:** Before production edits, `.venv/bin/python -m pytest
  tests/unit/config/test_settings.py tests/unit/db/test_indexing_models.py
  tests/unit/indexing/test_import_boundaries.py
  tests/unit/indexing/test_keywords.py
  tests/unit/indexing/test_segment_persistence.py
  tests/unit/indexing/test_document_engine.py
  tests/unit/vector_stores/test_milvus_store.py -q` reported `28 failed, 169
  passed, 2 warnings`: A accounted for twelve failures, B one, C nine, D one,
  E three, F3 one, and F4 one. F1 and F2 passed as valid characterization.
  Strengthened early batch validation then reported `4 failed`, and the Alembic
  engine-boundary regression separately reported `1 failed`. These were the
  expected missing contracts, not environment failures.
- **TDD GREEN and verification:** A/B/C selectors passed `46 passed, 2
  warnings`; D selectors passed `5 passed, 2 warnings`; E/F selectors passed
  `27 passed, 2 warnings`; and the Alembic boundary passed `1 passed, 1
  warning`. The combined new focused set passed `201 passed, 2 warnings in
  2.25s`. Repository/indexing/Embedding/vector/config/db unit scopes passed
  `251 passed, 2 warnings in 2.53s`; parser/segmentation regressions passed `177
  passed, 1 warning in 0.78s`; the default integration-disabled Milvus module
  passed `1 passed, 2 skipped, 1 warning`. Direct `py_compile` and `git diff
  --check` exited zero. One fresh default suite passed `561 passed, 3 skipped,
  2 warnings in 8.39s`; skips remain opt-in Milvus/MinIO and warnings are the
  existing Starlette/httpx and jieba/pkg_resources deprecations. A final
  post-documentation verification is recorded in the phase fix report.
- **Commits:** Production and regression coverage are in `de2e4b2` (`fix:
  harden phase 4 indexing contracts`). The documentation/status update is in
  `docs: record phase 4 final fix wave`, the commit containing this section.
- **Residual risks:** PostgreSQL dialect compilation deterministically proves
  statement shape and bind counts, but no live PostgreSQL load/concurrency test
  ran in this wave. Existing SQLite race tests cover exact and incompatible
  contenders. Completed-row retry relies on persisted acknowledgement until
  Phase 5 reconciliation. Adaptive HTTP splitting can still make a configured
  engine unit more than one transport request after a size rejection. Fixed
  storage errors intentionally omit backend diagnostics, so deployments need a
  separate sanitized operator-only signal. No live Docker run was warranted
  because production Milvus adapter behavior changed only by the direct integer
  bound.

### 2026-09-05 — Phase 4 final scoped re-review and controller verification

- **Review result:** The same independent final reviewer re-examined only the
  permitted final-fix range `600db7e..8f0ec6f`. Findings A-E and F1-F6 were all
  marked resolved. The reviewer confirmed that `8f0ec6f` is evidence-only and
  accurately describes the 10,000-record test as twenty inserts, twenty initial
  existence reads, and twenty post-insert reloads. No new Critical or Important
  issue was found; the verdict was `Ready to merge: Yes`.
- **Process symptom:** After completing technical inspection, the review turn
  remained active through repeated 60- and 120-second waits and did not render
  its verdict. A normal convergence reminder did not resolve the delay.
- **Ruling and recovery:** The controller interrupted the hanging response and
  resumed the same reviewer with a strict instruction to run no more commands,
  read no new files, and only format the already-collected A-F evidence. This
  remained the single allowed scoped re-review; no second reviewer, fix wave,
  test mutation, or repository mutation was introduced. The recovery produced
  the complete itemized verdict above.
- **Independent final evidence:** The controller, rather than relying on the
  implementer's report, reran the default suite (`561 passed, 3 skipped, 2
  warnings`), Embedding/indexing/vector units (`217 passed, 2 warnings`), and
  repository/config/database units (`34 passed, 1 warning`). The integration
  module in default opt-out mode reported `1 passed, 2 skipped, 1 warning`.
  With explicit approval to access the existing local service, the live Milvus
  2.5.14 suite reported `3 passed, 1 warning`; its tests created and cleaned only
  generated owned collections. `compileall` and `git diff --check
  28b3cb0..8f0ec6f` exited zero, and tracked worktree status was clean.
- **Known non-blockers:** The two warnings remain the existing Starlette/httpx
  and jieba/pkg_resources deprecations. The default skips remain the explicit
  opt-in Milvus and MinIO tests. This final run does not add live PostgreSQL load
  evidence; PostgreSQL statement shape/bind bounds remain covered through the
  real dialect compiler and retry races through the SQLite concurrency tests.
