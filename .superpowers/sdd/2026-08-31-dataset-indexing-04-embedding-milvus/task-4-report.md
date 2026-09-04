# Task 4 Report — Stable Segment Persistence

## Design self-check (approved design applied)

This task implements only the PostgreSQL staging boundary for deterministic
`document_segments`. The repository will use transaction-neutral `flush`
operations so Phase 5 can control activation and index-version switching in its
own explicit transaction. It will normalize content by converting CRLF/CR to LF
and applying Unicode NFC; metadata will be recursively NFC/newline-normalized,
JSON-safe, key-sorted, and encoded with compact canonical JSON. SHA-256 hashes
the canonical object containing both values. UUIDv5 IDs use a fixed application
namespace and include dataset-index version, document, deterministic parent ID,
position, and content hash. Parent links are fully validated and resolved in
memory before persistence, including child-before-parent preview order. No
vector value exists in this persistence path, is written to PostgreSQL, or is
logged.

## Implementation

- `rag_modules/indexing/models.py` adds frozen `SegmentStagingCommand`, the
  command snapshot consumed by `SegmentRepository.stage(command, segments)`.
- `rag_modules/indexing/ids.py` documents and implements deterministic NFC/LF
  content and metadata normalization, canonical compact sorted JSON, SHA-256
  content hashing, and compact UUIDv5 segment IDs under one fixed application
  namespace. Metadata arrays preserve order; mapping keys sort after string
  normalization; non-JSON values, non-string keys, normalized duplicate keys,
  and non-finite numbers are rejected.
- `rag_modules/repositories/segment_repository.py` stages deterministic segment
  records with `flush` but no `commit`; validates every preview and all parent
  links before adding any row; computes parent IDs in a first pass, then resolves
  children regardless of preview order; detects immutable-identity/content-hash
  conflicts; and returns an existing exact retry without mutating its original
  job attribution. It exposes narrowly scoped activation and previous-version
  soft deletion for the Phase 5 coordinator without invoking them itself.
- `rag_modules/db/models.py` retains PostgreSQL `ARRAY(Text)` for `keywords`
  (and therefore its existing PostgreSQL GIN index) but maps only SQLite to JSON.
  This was necessary for the required real async-SQLite behavior tests because
  SQLite cannot compile PostgreSQL `ARRAY` DDL. It adds no PostgreSQL column,
  migration, or vector storage.
- `rag_modules/indexing/__init__.py` exports the staging command.

No repository function accepts, writes, returns, or logs a vector. The existing
`DocumentSegmentRecord` schema remains the storage target and no engine, Celery,
Milvus, source-sync, or migration work was added.

## Tests

`tests/unit/indexing/test_segment_persistence.py` uses a real `aiosqlite`
`AsyncSession`, not mocks, and covers:

- retry-stable/version-sensitive compact UUIDv5 IDs;
- canonical newline, Unicode NFC, metadata-key-order stable hashes;
- parent-to-persisted-ID resolution, including a child preview listed before its
  parent;
- exact retry idempotency and content-hash collision conflict rejection;
- missing/non-parent links rejected before a row is added;
- high-quality/economy/parent embedding-status and staging-status rules;
- explicit economy/parent-child configuration rejection;
- activation scope; and
- previous-version-only soft deletion with aware UTC timestamps.

## TDD log and encountered symptoms

1. Initial RED command:

   ```text
   .venv/bin/python -m pytest tests/unit/indexing/test_segment_persistence.py -v
   ```

   collected zero tests and failed with
   `ModuleNotFoundError: No module named 'rag_modules.indexing.ids'`, proving the
   test addressed an absent persistence layer.
2. The first implementation run exposed two test/ORM integration symptoms:
   the strict asyncio plugin required `@pytest_asyncio.fixture` rather than a
   plain async pytest fixture, and SQLite failed table DDL with
   `UnsupportedCompilationError ... ARRAY(Text())`. The fixture correction was
   test-only. The latter was a proven dialect mapping defect in the existing ORM,
   fixed by a SQLite-only JSON variant while preserving PostgreSQL ARRAY.
   Test-local hand-written tables and mock repositories were rejected because
   they would not exercise the real mapped `DocumentSegmentRecord`/async SQL
   behavior requested for this task.
3. After the initial green baseline, the economy/parent-child rule was added
   test-first. Its RED run reported `1 failed, 11 passed` with `DID NOT RAISE
   SegmentPersistenceError`; adding the command-level design validation turned
   the unchanged suite green.
4. GREEN command:

   ```text
   .venv/bin/python -m pytest tests/unit/indexing/test_segment_persistence.py -v
   ```

   Result: `12 passed, 2 warnings in 0.08s`. The warnings are the existing
   Starlette/httpx and jieba/pkg_resources deprecations.
5. Relevant regression command:

   ```text
   .venv/bin/python -m pytest tests/unit/db/test_indexing_models.py tests/unit/repositories/test_document_repository.py tests/unit/segmentation tests/unit/indexing/test_keywords.py -v
   ```

   Result: `65 passed, 2 warnings in 0.94s`.

## Self-review and scope decisions

- The immutable identity check deliberately excludes `indexing_job_id`: retry
  IDs must remain reusable across retry jobs, and the existing row is never
  silently rewritten to a new job.
- `stage`, activation, and deletion only `flush`; later orchestration owns the
  commit and must invoke activation only after vector validation, then delete a
  prior version only after the new records are active.
- A child links to the deterministic persisted parent ID, never to the preview
  local ID. Validation completes before the first add, so malformed parent
  references cannot produce a partial write.
- Direct vectors are intentionally absent from all candidate/record fields and
  logs. PostgreSQL segment vector persistence remains `None`/out of scope.
- Rejected approaches: random IDs, JSON insertion-order hashes, hashing only
  content, parent insertion-order dependency, upserting by mutation, automatic
  activation in staging, and test assertions against repository mocks.

## Final verification, commit, and residual risks

The final pre-commit verification commands completed successfully:

```text
.venv/bin/python -m py_compile rag_modules/indexing/__init__.py rag_modules/indexing/ids.py rag_modules/indexing/models.py rag_modules/repositories/segment_repository.py rag_modules/db/models.py tests/unit/indexing/test_segment_persistence.py
git diff --check
.venv/bin/python -m pytest -v
```

`py_compile` and `git diff --check` exited with no output/errors. The one fresh
full suite reported `468 passed, 1 skipped, 2 warnings in 6.96s`; the skipped
test is the opt-in MinIO integration test, and the warnings are the existing
Starlette/httpx and jieba/pkg_resources deprecations.

Final self-review found no vector field or payload in this change, no commit in
the repository API, and no activation call from any engine (none was added).
The remaining bounded risk is concurrent identical staging in separate database
transactions: PostgreSQL's primary key is the ultimate arbiter, and Phase 5
should retry/reload on a uniqueness race within its owning transaction. SQLite
validates observable persistence behavior but does not model PostgreSQL row
locking or the final cross-store activation transaction.

Final Task-4 commit: `feat: stage deterministic document segments` (the commit
SHA is supplied with this task handoff).

## Fix Round 1 — race-safe deterministic staging

### Finding, reproduction, and root cause

The original `stage` implementation selected candidate IDs, added ORM objects,
then flushed. Two transactions could both observe no row, after which the loser
received a primary-key `IntegrityError`. Because that flush was not protected by
a savepoint, SQLAlchemy marked the caller-owned transaction as failed; this was
neither idempotent success nor the required safe conflict error.

The test suite now creates a real SQLite database file with an `AsyncSession`
for the repository and a separate synchronous connection. A SQLAlchemy cursor
event commits an interloper exactly when the repository begins the target insert
(no sleep or mock timing). The exact interloper has the same immutable fields
and hash; the conflict interloper reuses the deterministic ID with incompatible
content/hash. Each test then stages a separate second document through the same
session, proving the caller's outer transaction remains usable. Timestamp tests
now refresh records from SQLite before asserting that timestamp values persisted;
they no longer claim SQLite returns timezone-aware datetimes, because SQLite's
datetime storage does not preserve that distinction. Production values still
come from aware UTC `utcnow`.

Initial RED command:

```text
.venv/bin/python -m pytest tests/unit/indexing/test_segment_persistence.py -v
```

Result: `2 failed, 12 passed, 2 warnings`. Both new tests reached the expected
`sqlite3.IntegrityError: UNIQUE constraint failed: document_segments.id` from
`SegmentRepository.stage`'s unprotected ORM flush. This proves the failure was
at the select/add/flush transaction boundary rather than a test fixture or
parent/link calculation.

### Design and implementation

The fix replaces the add/flush batch with one dialect-native batch upsert:

- PostgreSQL uses `INSERT ... ON CONFLICT (id) DO NOTHING`.
- SQLite uses the equivalent `ON CONFLICT (id) DO NOTHING` for the real async
  behavior test environment.
- The repository then reloads **every** requested candidate and reapplies the
  complete immutable-identity/content hash/normalized content/metadata check
  before returning records in preview order.

This is one statement for the missing batch plus reloads, not an O(n) per-row
savepoint design. Expected ID collisions never raise, so there is no rollback
of the caller's outer transaction. Any non-unique `IntegrityError`, SQL/program
error, or unsupported dialect still propagates normally rather than being
misclassified as a retry. The method remains caller-owned and commit-free: the
upsert is part of the current transaction and makes no commit. A matching row is
an idempotent success; a row with any incompatible immutable identity safely
raises `SegmentPersistenceError` after reload.

Rejected approaches: catching all `IntegrityError` values (would hide foreign
key/check/program errors), per-row nested savepoints (inefficient and no need
when the database can atomically ignore the intended PK conflict), and a timing
dependent two-coroutine test (flaky under SQLite locking). A savepoint recovery
would preserve an outer transaction, but the native upsert removes the expected
error path while also handling an entire batch efficiently.

### GREEN and verification

Unchanged focused command after implementation: `14 passed, 2 warnings in
0.10s`. The added tests cover exact race recovery, conflicting race safe failure,
and continued use of the same outer session. Relevant db/repository/
segmentation/keyword regressions passed `65 passed, 2 warnings in 0.94s`.
`py_compile` of the repository and persistence tests and `git diff --check`
exited successfully. The two warnings remain the existing Starlette/httpx and
jieba/pkg_resources deprecations.

Final verification command:

```text
.venv/bin/python -m py_compile rag_modules/repositories/segment_repository.py tests/unit/indexing/test_segment_persistence.py
git diff --check
.venv/bin/python -m pytest -v
```

`py_compile` and `git diff --check` completed with no output/errors. The fresh
full suite passed `470 passed, 1 skipped, 2 warnings in 7.04s`; the skip is the
opt-in MinIO integration test and warnings are unchanged. Final review confirms
that the race path has no exception catch, no commit, no vector behavior, and
reloads every candidate before returning.

Final fix commit: `fix: make segment staging race-safe` (the SHA is supplied
with this task handoff). Residual risk: SQLite validates deterministic conflict
behavior, but PostgreSQL production deployment is still responsible for its
isolation and lock-timeout configuration. Under PostgreSQL Read Committed, `ON
CONFLICT` waits for a conflicting transaction to decide, and the following
reload sees the committed winner; an aborted competing transaction permits this
insert instead.
