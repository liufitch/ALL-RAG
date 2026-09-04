"""Deterministic identities and content hashes for persisted document segments.

Content normalization converts CRLF/CR to LF and applies Unicode NFC. Metadata
normalization applies the same treatment recursively to JSON string values and
mapping keys, preserves array order, requires finite JSON numbers, and encodes
the result with sorted keys and compact separators. The SHA-256 input is a
canonical JSON object with exactly ``content`` and ``source_metadata`` keys.
"""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID, uuid5


_SEGMENT_ID_NAMESPACE = UUID("cbd3960d-2c2d-52e8-b4de-5f37e8e9dc75")


def normalize_segment_content(content: str) -> str:
    """Return the canonical text representation used for hashes and storage."""
    if not isinstance(content, str):
        raise TypeError("segment content must be a string")
    return unicodedata.normalize("NFC", content.replace("\r\n", "\n").replace("\r", "\n"))


def canonical_source_metadata(source_metadata: Mapping[str, Any]) -> str:
    """Serialize metadata deterministically after validating JSON-compatible values."""
    normalized = _normalize_metadata(source_metadata, path="source_metadata")
    if not isinstance(normalized, dict):  # defensive: mappings normalize to dictionaries
        raise TypeError("source_metadata must be a mapping")
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def normalized_source_metadata(source_metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Return a detached canonical metadata value suitable for the JSON column."""
    return json.loads(canonical_source_metadata(source_metadata))


def segment_content_hash(content: str, source_metadata: Mapping[str, Any]) -> str:
    """SHA-256 of normalized content and canonical source metadata."""
    payload = json.dumps(
        {
            "content": normalize_segment_content(content),
            "source_metadata": _normalize_metadata(source_metadata, path="source_metadata"),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def stable_segment_id(
    dataset_index_id: str,
    document_id: str,
    parent_id: str | None,
    position: int,
    content_hash: str,
) -> str:
    """Return the retry-stable UUIDv5 hex identity for a segment version."""
    _require_identifier("dataset_index_id", dataset_index_id)
    _require_identifier("document_id", document_id)
    if parent_id is not None:
        _require_identifier("parent_id", parent_id)
    if isinstance(position, bool) or not isinstance(position, int) or position < 0:
        raise ValueError("position must be a non-negative integer")
    if not isinstance(content_hash, str) or not content_hash:
        raise ValueError("content_hash must be a non-empty string")
    canonical_identity = json.dumps(
        [dataset_index_id, document_id, parent_id, position, content_hash],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return uuid5(_SEGMENT_ID_NAMESPACE, canonical_identity).hex


def _normalize_metadata(value: Any, *, path: str) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return normalize_segment_content(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{path} contains a non-string key")
            normalized_key = normalize_segment_content(key)
            if normalized_key in normalized:
                raise ValueError(f"{path} contains duplicate normalized keys")
            normalized[normalized_key] = _normalize_metadata(
                child, path=f"{path}.{normalized_key}"
            )
        return normalized
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [_normalize_metadata(child, path=f"{path}[]") for child in value]
    raise TypeError(f"{path} contains a non-JSON value")


def _require_identifier(name: str, value: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 36:
        raise ValueError(f"{name} must be a non-empty string of at most 36 characters")
