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
