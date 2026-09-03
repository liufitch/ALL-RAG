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
