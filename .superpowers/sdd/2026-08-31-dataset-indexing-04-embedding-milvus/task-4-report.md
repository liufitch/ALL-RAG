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
