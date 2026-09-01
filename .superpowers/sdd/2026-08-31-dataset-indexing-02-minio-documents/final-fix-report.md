# MinIO document phase final-fix report

Date: 2026-09-01

Plan: `docs/superpowers/plans/2026-08-31-dataset-indexing-02-minio-documents.md`

Spec: `docs/superpowers/specs/2026-08-31-dify-style-dataset-indexing-design.md`

Base HEAD: `58666ab40938ceea083fba7824fdef3f085ea4a8`

Scope: final-review corrections for the MinIO-backed document upload phase only.
No parser, splitter, embedding, indexing, worker, or vector-store behavior was
added or changed.

## Important finding 1: typed MinIO failure boundary

### Root cause

The installed MinIO 7 SDK hierarchy was inspected directly. `S3Error`,
`InvalidResponseError`, and `ServerError` are sibling subclasses of
`MinioException`. The adapter caught the first two concrete subclasses but not
`ServerError` or another future/base `MinioException`. Consequently a MinIO 5xx
could escape `MinioObjectStorage._run`, bypass `ObjectStorageUnavailable`, and
reach the document route as HTTP 500 instead of the required infrastructure
HTTP 503.

### Correction

- The adapter now catches the SDK's `MinioException` base class together with
  the existing transport/OS exception families and raises
  `ObjectStorageUnavailable` with the SDK exception retained as `__cause__`.
- `remove_object` still inspects an `S3Error` cause and treats `NoSuchKey` as a
  successful idempotent removal.
- A real ASGI request backed by a fake MinIO client proves `ServerError(503)` is
  normalized by the adapter and classified by the API as HTTP 503.
- Adapter coverage now also proves missing-bucket creation and `get_stream`
  close/release behavior on normal and consumer-exception exits.

### TDD evidence

Initial focused RED command:

```console
.venv/bin/python -m pytest tests/unit/object_storage/test_minio_store.py \
  tests/api/test_document_api.py::test_document_upload_maps_minio_server_failure_to_503 -v
# 3 failed, 5 passed, 1 warning
```

The two adapter cases leaked `ServerError`/base `MinioException`, and the ASGI
case returned 500 instead of 503. After the minimal base-class catch:

```console
.venv/bin/python -m pytest tests/unit/object_storage/test_minio_store.py \
  tests/api/test_document_api.py::test_document_upload_maps_minio_server_failure_to_503 -v
# 8 passed, 1 warning
```

## Important finding 2: immutable upload-format ceiling

### Root cause

`prepare_upload` previously treated `UploadSettings.allowed_extensions` as the
complete authority. An operator could add `.exe`; because that extension had
no MIME, signature, text, or container policy, the upload passed validation.
There was also no settings-time rejection for formats outside the approved
seven.

### Correction

- `rag_modules/upload_formats.py` defines the one immutable approved tuple, in
  the required order: `.txt`, `.md`, `.pdf`, `.docx`, `.xls`, `.xlsx`, `.csv`.
- `UploadSettings` uses the tuple as its default, normalizes configured values,
  deduplicates them, and raises a clear settings validation error for extras.
- Runtime validation independently intersects configured extensions with the
  immutable tuple. This defense remains effective even if Pydantic validation
  is bypassed or a nonstandard settings object reaches `prepare_upload`.
- Configuration can still deliberately narrow the set; a `.txt`-only setting
  accepts `.txt` and rejects an otherwise approved `.md`.

### TDD evidence

Settings RED:

```console
.venv/bin/python -m pytest \
  tests/unit/config/test_settings.py::test_upload_settings_reject_extensions_outside_approved_formats -v
# 1 failed: .exe did not raise
```

Settings GREEN after the validator and neutral constant were applied:

```console
.venv/bin/python -m pytest \
  tests/unit/config/test_settings.py::test_upload_settings_reject_extensions_outside_approved_formats -v
# 1 passed, 1 warning
```

Runtime/MIME RED:

```console
.venv/bin/python -m pytest \
  tests/unit/documents/test_upload_validation.py::test_prepare_upload_does_not_allow_configuration_to_add_formats \
  tests/unit/documents/test_upload_validation.py::test_prepare_upload_honors_configured_format_narrowing \
  tests/unit/documents/test_upload_validation.py::test_prepare_upload_rejects_contradictory_fixed_format_mime \
  tests/unit/documents/test_upload_validation.py::test_prepare_upload_accepts_octet_stream_for_fixed_formats -v
# 5 failed, 5 passed, 1 warning
```

The injected `.exe` and four contradictory `image/png` cases were accepted;
narrowing and octet-stream fallback already passed. Runtime/MIME GREEN:

```console
# same command
# 10 passed, 1 warning
```

## Bounded adjacent review fixes

- Fixed-signature formats now accept only their standard MIME type or an
  explicit `application/octet-stream` fallback. Contradictory MIME is rejected
  before reading/storing content. Existing magic and ZIP safety checks remain
  unchanged and are still exercised.
- The archive entry-count safety branch now has a direct 10,001-entry ZIP test;
  it completes in well under one second and does not extract entries.
- Runtime-unused `DATA_FILE = ...knowledge_bases.json`, unused legacy imports,
  and the entire commented JSON/Milvus API block were removed from `main.py`.
  The active FastAPI construction and all three active router registrations are
  retained and covered by route regressions.

## Verification evidence

Expanded phase-core command (the former 41-test command plus new cases):

```console
.venv/bin/python -m pytest tests/unit/object_storage tests/unit/documents \
  tests/unit/services/test_document_service.py tests/api/test_document_api.py -v
# 58 passed, 1 warning in 0.49s
```

Settings-focused suite:

```console
.venv/bin/python -m pytest tests/unit/config/test_settings.py -v
# 4 passed, 1 warning in 0.01s
```

Exact dataset/indexing-option/route regression command:

```console
.venv/bin/python -m pytest tests/api/test_dataset_api.py \
  tests/api/test_indexing_options.py tests/test_api_routes.py -v
# 22 passed, 1 warning in 0.38s
```

Live MinIO integration, using the existing local endpoint and the fixture's
UUID-owned prefix cleanup only:

```console
RUN_INTEGRATION=1 TEST_MINIO_ENDPOINT=localhost:9000 \
  TEST_MINIO_ACCESS_KEY=minioadmin TEST_MINIO_SECRET_KEY=minioadmin \
  TEST_MINIO_BUCKET=graph-rag-integration-tests TEST_MINIO_SECURE=false \
  .venv/bin/python -m pytest tests/integration/test_minio_documents.py -v
# 1 passed, 1 warning in 0.02s
```

No bucket, container, volume, or shared object prefix was removed.

Repository-wide default test run:

```console
.venv/bin/python -m pytest tests -q
# 91 passed, 1 skipped, 1 warning in 0.84s
```

The single skip is the deliberately gated live MinIO integration, which passed
in the separately enabled command above. `git diff --check` also exited 0 with
no output.

## Self-review and deferred concerns

No Critical or Important finding remains in this final-fix scope. The following
items remain deliberately deferred as directed:

- missing-key `get_stream` domain semantics, because no current API consumes
  reads;
- concurrent `position` allocation, Python-side JSON duplicate matching, and
  durable cleanup/outbox behavior;
- storage-factory cache invalidation for a future runtime configuration reload;
- the Starlette/httpx deprecation warning, which is an upstream dependency
  migration concern.

No concurrency, persistence architecture, parser/indexer, or dependency
redesign was attempted in this wave.

## Recovery audit after interrupted final-fix wave

Recovery owner audit date: 2026-09-01

The interrupted work was recovered in place from
`58666ab40938ceea083fba7824fdef3f085ea4a8`. No checkout, reset, wholesale
revert, worktree replacement, or unrelated-file cleanup was performed.

### Working-tree and `main.py` scope audit

- The initial recovery status contained exactly the eight reported modified
  files plus the new `rag_modules/upload_formats.py`; no unrelated modified,
  deleted, or untracked path was present.
- `main.py` was compared line by line with the base version. Its active
  `FastAPI` construction and all three router registrations remain unchanged.
  The router-registration comment, `CORSMiddleware` import/commented
  configuration, and commented `/api/health` scaffold were restored after the
  first interrupted draft had removed them too broadly.
- The resulting `main.py` delta is 6 insertions and 374 deletions. The deletions
  are limited to imports/assignments used only by the legacy block, stale
  `DATA_FILE`/frontend/embedding constants, the duplicate unused
  `safe_collection_name`, and commented legacy JSON, Milvus, and static-file
  handlers. No active application or router code was removed.
- No Python tutorial/example content or file outside the nine expected
  code/test paths and this report was edited.
- The explicitly deferred position, duplicate, cleanup/outbox, cache-reload,
  and warning items remain untouched.

### Recovered MinIO boundary verification

The adapter test now names and exercises each required family: MinIO 7
`ServerError`, authentication `S3Error(AccessDenied)`, the `MinioException`
base, and a urllib3 `NewConnectionError`. Every case is normalized to
`ObjectStorageUnavailable` with the original exception retained as the cause.
The existing `NoSuchKey` removal case still completes idempotently, and the
real ASGI request still proves that a `ServerError(503)` becomes HTTP 503.

Final focused adapter/API command after the recovery additions:

```console
.venv/bin/python -m pytest tests/unit/object_storage/test_minio_store.py \
  tests/api/test_document_api.py::test_document_upload_maps_minio_server_failure_to_503 -v
# 10 passed, 1 warning in 0.02s
```

The supported-format focused commands were also rerun on the recovered tree:

```console
.venv/bin/python -m pytest \
  tests/unit/config/test_settings.py::test_upload_settings_reject_extensions_outside_approved_formats -v
# 1 passed, 1 warning in 0.00s

.venv/bin/python -m pytest \
  tests/unit/documents/test_upload_validation.py::test_prepare_upload_does_not_allow_configuration_to_add_formats \
  tests/unit/documents/test_upload_validation.py::test_prepare_upload_honors_configured_format_narrowing \
  tests/unit/documents/test_upload_validation.py::test_prepare_upload_rejects_contradictory_fixed_format_mime \
  tests/unit/documents/test_upload_validation.py::test_prepare_upload_accepts_octet_stream_for_fixed_formats -v
# 10 passed, 1 warning in 0.02s
```

These confirm that `.exe` cannot be enabled even through an unvalidated runtime
settings object, while a configured `.txt`-only narrowing accepts `.txt` and
rejects `.md`. The immutable tuple remains exactly `.txt`, `.md`, `.pdf`,
`.docx`, `.xls`, `.xlsx`, `.csv`.

### Final recovered-tree verification

```console
.venv/bin/python -m pytest tests/unit/object_storage tests/unit/documents \
  tests/unit/services/test_document_service.py tests/api/test_document_api.py -v
# 60 passed, 1 warning in 0.52s

.venv/bin/python -m pytest tests/unit/config/test_settings.py -v
# 4 passed, 1 warning in 0.01s

.venv/bin/python -m pytest tests/api/test_dataset_api.py \
  tests/api/test_indexing_options.py tests/test_api_routes.py -v
# 22 passed, 1 warning in 0.34s

RUN_INTEGRATION=1 TEST_MINIO_ENDPOINT=localhost:9000 \
  TEST_MINIO_ACCESS_KEY=minioadmin TEST_MINIO_SECRET_KEY=minioadmin \
  TEST_MINIO_BUCKET=graph-rag-integration-tests TEST_MINIO_SECURE=false \
  .venv/bin/python -m pytest tests/integration/test_minio_documents.py -v
# 1 passed, 1 warning in 0.02s

.venv/bin/python -m pytest tests -q
# 93 passed, 1 skipped, 1 warning in 0.75s

.venv/bin/python -m compileall -q main.py rag_modules tests
# exit 0, no output

git diff --check
# exit 0, no output
```

The live integration used only the fixture-generated UUID object prefix and
removed its object. It did not remove the bucket, a container, a volume, or a
shared prefix. The default-suite skip is that same deliberately gated live
integration test. The only warning remains the already-deferred
Starlette/httpx deprecation warning.
