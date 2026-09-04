# Task 6 Report — Real Milvus 2.5.14 Integration

## Outcome

Completed isolated real-service verification and two minimal adapter
compatibility repairs discovered by that verification. The project virtualenv
uses PyMilvus 3.0.1 with the Compose target Milvus 2.5.14; the project remains
on its declared `pymilvus>=2.5.0` dependency without a pin or downgrade.

## Integration coverage

- Added `real_milvus_store`, gated strictly on `RUN_INTEGRATION=1`, with an
  explicit localhost/default-database configuration, no token/user/password,
  bounded `connect_timeout`, batch size, and condition-poll settings.
- Added `tests/integration/test_milvus_index.py`. Every test creates its own
  generated `test_<uuid>` collection and drops only that name in `finally`.
- The main test creates and revalidates the adapter’s explicit schema and
  HNSW/COSINE index, performs two upserts of one generated stable ID, proves
  logical count is one, deletes that ID, proves count zero, and proves the
  owned collection no longer exists after drop.
- The second test deliberately raises an assertion inside the collection body
  and proves the owning `finally` path drops it. It does not leak a collection
  to demonstrate cleanup.
- With `RUN_INTEGRATION` absent, the module reports `2 skipped, 1 warning`.

## Live evidence and compatibility findings

The initial sandboxed client was unable to reach localhost Docker-published
ports. Elevated execution reached the existing healthy standalone service; an
HTTP health probe returned `OK`.

1. **Schema metadata shape:** The first live test created a collection but the
   second `ensure_collection` rejected it. A generated owned diagnostic
   collection showed that PyMilvus 3.0.1’s response from Milvus 2.5.14 omits
   `nullable` for non-nullable fields, while preserving `nullable=True` for
   `parent_id`. The adapter previously required explicit `False`. A focused
   unit RED (`1 failed, 50 deselected`) reproduced the observed shape. The
   minimal repair treats only missing `nullable` as the Milvus default `False`;
   incorrect explicit values and a missing nullable value for `parent_id`
   remain schema mismatches.

2. **Physical versus logical count:** After two same-ID upserts, physical
   `get_collection_stats().row_count` was stably two, while a primary-key query
   returned one logical entity. A generated owned diagnostic established that
   `query(output_fields=['count(*)'])` returns the correct logical count. The
   observed PyMilvus return is a `HybridExtraList`, iterable/indexable as one
   `{'count(*)': N}` mapping. A focused count RED (`1 failed, 50 deselected`)
   then passed after `count()` retained flush plus its two-equal-observations
   polling and read that one-row aggregate instead of physical row statistics.

Every diagnostic used a generated test collection, dropped it in `finally`, and
proved only its own absence (`cleanup_exists=False`). No arbitrary collection
inventory, deletion, volume removal, prune, or `docker compose down -v` was
performed. Raw backend errors, credentials, and tokens are absent from test
output and this report.

## Compose and service status

- Compose interpolates the whole file before service selection. In the
  worktree with no `.env`, process-only benign placeholders were needed for
  unrelated required RabbitMQ/Neo4j/PostgreSQL interpolation plus dedicated
  MinIO credentials. No `.env` was created.
- Direct sandbox Docker access was denied at the Docker socket and was retried
  only with approved escalation.
- Starting a separate worktree stack was blocked by the pre-existing fixed
  name `milvus-etcd`. It was not removed or renamed. Targeted status inspection
  found the primary-worktree `graph-rag` shared services:

  | Service | Image | State |
  | --- | --- | --- |
  | etcd | `quay.io/coreos/etcd:v3.5.18` | Up, healthy |
  | standalone | `milvusdb/milvus:v2.5.14` | Up, healthy |
  | minio | `minio/minio:RELEASE.2024-05-28T17-19-04Z` | Up, unhealthy |

  The application MinIO adapter is not directly exercised and MinIO was not
  changed. Milvus standalone nevertheless remains configured to use this
  pre-existing MinIO storage backend, so its unhealthy healthcheck and the use
  of a shared stack are residual environment concerns even though the vector
  path passed.

## Verification

- Disabled integration before Fix Round 1: `2 skipped, 1 warning`.
- Focused schema/count/mismatch units: `14 passed, 37 deselected, 1 warning`.
- Full Task 3 vector units: `51 passed, 1 warning`.
- Phase unit scope (`embeddings`, `indexing`, `vector_stores`): `184 passed,
  2 warnings`.
- Live service evidence: controller-run approved live command reported schema
  subset `8 passed, 43 deselected`; `RUN_INTEGRATION=1` integration reported
  `2 passed, 1 warning in 13.96s` against the healthy `milvusdb/milvus:v2.5.14`
  service.
- `py_compile` for the changed adapter and unit module: passed.
- `git diff --check`: passed.
- Fresh default suite before Fix Round 1: `523 passed, 3 skipped, 2 warnings in 7.11s`. The skips
  are the two opt-in Milvus tests and one opt-in MinIO test. Warnings are the
  existing Starlette/httpx and jieba/pkg_resources deprecations.

## Rejected approaches and residual risk

Pinning/downgrading PyMilvus, accepting physical `row_count` as logical entity
count, arbitrary sleeps, deleting shared containers/volumes, starting unrelated
Compose services, and broad cleanup enumeration were rejected. Future SDK
response shapes can still change, but the two observed live shapes now have
focused regressions. Task 5’s per-document acknowledged-upsert-count rule
remains appropriate for concurrent document indexing; collection-level logical
count is the adapter API behavior verified here.

## Fix Round 1 — cleanup verification sanitization

An independent review found that the test-only `_collection_exists()` helper
called the private raw client and could allow a `MilvusException` to print a
backend message in pytest output. A no-live fake-client RED was added first:
the fake raised a MilvusException with a distinctive backend detail and the
test failed with that exact raw exception (`1 failed, 2 deselected`).

The helper now catches only `MilvusException` and raises the fixed generic
`AssertionError("Milvus cleanup verification failed.") from None`. It does not
catch arbitrary programmer exceptions. The GREEN test asserts the fixed text,
the absence of the distinctive detail, no explicit cause, and suppressed
context. The test-owned collection cleanup evidence remains unchanged.

- Focused sanitizer GREEN: `1 passed, 2 deselected, 1 warning`.
- Disabled module after adding the no-live regression: `1 passed, 2 skipped,
  1 warning`.
- Live integration after the sanitizer change: `3 passed, 1 warning in
  14.19s` against the existing healthy Milvus 2.5.14 service.
- Full Task 3 vector units after the change: `51 passed, 1 warning`.
- Phase unit scope after the change: `184 passed, 2 warnings`.
- Fresh default suite after the change: `524 passed, 3 skipped, 2 warnings in
  7.18s`.
- `py_compile` and `git diff --check` after the change: passed.

This repair did not start Compose, alter containers, or mutate any live
collection outside the existing tests' generated ownership boundary.

## Commits

- `e563d68` — `fix: support live milvus metadata and counts`
- This task’s isolated integration fixture/test/report is committed separately
  as `test: verify real milvus indexing`.
