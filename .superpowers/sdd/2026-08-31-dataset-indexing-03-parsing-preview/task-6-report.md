# Task 6 Report — Real Preview Service and API

## Outcome

Implemented a read-only preview pipeline and registered
`POST /api/knowledge_base/{dataset_id}/indexing/preview`. The service validates
an active dataset and all requested documents, loads every document in one
dataset-scoped repository query, reads canonical MinIO objects within one
request deadline, and runs the complete parser registry plus the shared
`Segmenter` in worker threads. It returns bounded chunks while retaining full
segment counts, parser/segmenter warnings, document source metadata, and
cross-document-safe public IDs.

## RED

Initial command:

```text
.venv/bin/python -m pytest \
  tests/unit/services/test_preview_service.py \
  tests/api/test_indexing_preview_api.py \
  tests/unit/repositories/test_document_repository.py \
  tests/unit/parsing/test_registry_factory.py -q
```

Result: collection failed with the expected missing production interfaces:

```text
ModuleNotFoundError: No module named 'rag_modules.api.dto.indexing_preview'
ModuleNotFoundError: No module named 'rag_modules.parsing.factory'
2 errors during collection
```

Two self-review regressions were also proven RED before correction:

- a repository/infrastructure `TimeoutError` was incorrectly converted to
  `PREVIEW_TIMEOUT`;
- a string `"10"` was coerced into an integer segmentation limit instead of
  retaining FastAPI's standard Pydantic 422 response.

## GREEN and Coverage

The Task 6 suite covers:

- real `TextParser` plus the shared `Segmenter`;
- complete count with response truncation;
- parent-child totals counting parent and child records;
- multi-document order, duplicate normalization, and one repository query;
- missing, wrong-dataset, and deleted documents through the active lookup;
- unique document count, stored size, actual size, canonical MinIO key,
  original filename, storage provider, bucket, and approved suffix checks;
- parent-child with economy, invalid overlap, and unknown high-quality model;
- economy requests ignoring embedding-model resolution and never embedding;
- request-wide timeout, abandoned late parse completion, and independent
  in-memory stream ownership after the storage context closes;
- parser and segmentation warnings, parser document metadata, segment source
  metadata, and document-qualified public segment and parent IDs;
- all seven approved parser registrations;
- no repository write or vector-provider access;
- top-level domain `{code,message}`, standard Pydantic shape errors, 503
  infrastructure mapping, and 504 preview deadline mapping.

Target regression command:

```text
.venv/bin/python -m pytest tests/unit/parsing tests/unit/segmentation \
  tests/unit/services/test_preview_service.py \
  tests/api/test_indexing_preview_api.py \
  tests/unit/repositories/test_document_repository.py -q
```

Result:

```text
102 passed, 1 warning in 1.43s
```

The warning is the existing Starlette TestClient/httpx deprecation warning.

## Full Verification

Full suite:

```text
.venv/bin/python -m pytest -q
195 passed, 1 skipped, 1 warning in 2.03s
```

The skip is the existing opt-in MinIO integration test. The warning is the
same existing Starlette TestClient/httpx deprecation warning.

Compile and whitespace verification:

```text
.venv/bin/python -m compileall -q rag_modules main.py tests
git diff --check
```

Result: both commands exited 0 with no output.

## Self-review

- The object-storage stream is copied with an expected-size-plus-one bound
  while its async context is open. Parser and segmenter threads only receive a
  private `BytesIO`, so an abandoned late result cannot access a closed MinIO
  response.
- The fail-after scope covers active-dataset lookup, the single document
  query, all downloads, parsing, segmentation, counting, and serialization.
- Deadline mapping checks `CancelScope.cancel_called`; infrastructure
  `TimeoutError` remains available to the API's 503 mapping.
- Document metadata must exactly match the upload service's canonical key,
  configured MinIO bucket, database filename, approved suffix, and size.
- Production dependency construction includes only repositories for reads,
  object storage, parsers, and the pure segmenter. It does not construct a
  segment repository, embedding client, or vector store.
- DTOs use a discriminated segmentation union with strict coercion disabled
  and `extra="forbid"`.

## Commit

Planned message:

```text
feat: preview real document segments
```

## Concerns

- The repository's existing TestClient stack emits one deprecation warning
  because Starlette's compatibility layer recommends a future httpx upgrade.
- The live MinIO integration test remains opt-in and was skipped in the full
  suite; storage behavior is isolated with exact async stream fakes here and
  covered separately by the existing MinIO adapter tests.

## Fix Round 1 — End-to-end Preview Deadline

### Review findings addressed

1. A blocking object response read was awaited with
   `abandon_on_cancel=False` while the async stream context owned the response.
   This allowed the synchronous read to exceed the request deadline.
2. The route returned a Pydantic model and left response validation,
   serialization, and rendering to FastAPI after the service deadline ended.
3. High-quality preview used truthiness to choose the default embedding model,
   so an explicit empty string was silently replaced by the default.

The two deferred Minor findings recorded in the SDD ledger were not changed in
this fix round.

### RED evidence

Bounded object download and storage deadline:

```text
.venv/bin/python -m pytest \
  tests/unit/object_storage/test_minio_store.py::test_get_bytes_reads_at_most_requested_limit_and_releases_response \
  tests/unit/object_storage/test_minio_store.py::test_get_bytes_normalizes_read_failure_and_releases_response \
  tests/unit/object_storage/test_minio_store.py::test_get_bytes_deadline_abandons_owned_response_and_late_worker_releases \
  tests/unit/services/test_preview_service.py::test_slow_storage_returns_preview_timeout_near_deadline -q
```

Result: four failures. The adapter failures were the expected missing
`MinioObjectStorage.get_bytes`; the service returned `PREVIEW_TIMEOUT` only
after 1.60 seconds for a 1-second deadline, failing the `< 1.4` wall-clock
bound.

HTTP rendering deadline:

```text
.venv/bin/python -m pytest \
  tests/api/test_indexing_preview_api.py::test_preview_rendering_is_inside_route_deadline \
  tests/api/test_indexing_preview_api.py::test_dependency_timeout_is_still_infrastructure_503 -q
```

Result: one failure and one pass. A deliberately blocked
`PreviewResponse.model_dump` was never called by the handler, and the endpoint
returned 200 instead of the required 504, confirming rendering occurred after
the handler's service work.

Explicit empty embedding model:

```text
.venv/bin/python -m pytest \
  tests/unit/services/test_preview_service.py::test_explicit_empty_high_quality_model_is_not_replaced_by_default \
  tests/api/test_indexing_preview_api.py::test_explicit_empty_high_quality_model_is_domain_422 -q
```

Result: two failures. The service reached document lookup and returned
`DOCUMENT_NOT_FOUND`; the API reached infrastructure and returned 503 instead
of rejecting the empty model as `EMBEDDING_MODEL_UNAVAILABLE`.

### GREEN implementation and evidence

- Added `ObjectStorage.get_bytes(object_key, max_bytes)` and implemented MinIO
  download as one synchronous worker operation owning
  `get_object → bounded read → close → release_conn`. The async wait uses
  `abandon_on_cancel=True`; a late worker therefore owns and releases its own
  response without a close/read race.
- Preview requests exactly `expected_size + 1` bytes and retains the exact-size
  metadata check, so oversized content is detected without an unbounded read.
- Added a route-entry `fail_after` covering service execution and a worker
  operation that performs `PreviewResponse.model_validate`,
  `model_dump(mode="json")`, and `JSONResponse` construction/rendering.
  Successful handlers now return the rendered Response directly.
- Outer deadline cancellation maps to `PREVIEW_TIMEOUT`/504; a dependency's
  own `TimeoutError` with an uncancelled scope remains infrastructure 503.
- Only `embedding_model is None` selects the default. Explicit `""` and other
  unavailable IDs fail with `EMBEDDING_MODEL_UNAVAILABLE`; economy continues
  to skip model resolution.

Focused GREEN command:

```text
.venv/bin/python -m pytest \
  tests/unit/object_storage/test_minio_store.py \
  tests/unit/services/test_preview_service.py \
  tests/api/test_indexing_preview_api.py -q
```

Result:

```text
42 passed, 1 warning in 4.48s
```

### Final verification

Task 6, object storage, parser, and segmenter regression:

```text
.venv/bin/python -m pytest tests/unit/object_storage tests/unit/parsing \
  tests/unit/segmentation tests/unit/services/test_preview_service.py \
  tests/api/test_indexing_preview_api.py \
  tests/unit/repositories/test_document_repository.py -q
120 passed, 1 warning in 4.46s
```

Full suite:

```text
.venv/bin/python -m pytest -q
204 passed, 1 skipped, 1 warning in 5.04s
```

Compile, diff, and formatting checks:

```text
.venv/bin/python -m compileall -q rag_modules main.py tests
git diff --check
awk 'length($0)>99 {print FNR ":" length($0) ":" $0}' <changed files>
```

Result: all exited 0 with no output. The skip and warning remain the pre-existing
opt-in MinIO integration skip and Starlette TestClient/httpx deprecation
warning described above.

### Fix-round commit

Planned message:

```text
fix: enforce preview deadline end to end
```
