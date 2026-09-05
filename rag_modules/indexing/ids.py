"""为持久化文档分段生成确定性标识和内容哈希。

内容规范化将 CRLF/CR 转为 LF，并应用 Unicode NFC。元数据规范化会递归地
对 JSON 字符串值及映射键执行相同处理，保留数组顺序，要求 JSON 数值有限，
并按键排序、使用紧凑分隔符编码。SHA-256 的输入是一个规范化的 JSON 对象，
仅包含 ``content`` 和 ``source_metadata`` 两个键。
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
    """返回用于计算哈希和存储的规范化文本。"""
    if not isinstance(content, str):
        raise TypeError("segment content must be a string")
    return unicodedata.normalize("NFC", content.replace("\r\n", "\n").replace("\r", "\n"))


def canonical_source_metadata(source_metadata: Mapping[str, Any]) -> str:
    """校验值与 JSON 兼容后，以确定性的方式序列化元数据。"""
    normalized = _normalize_metadata(source_metadata, path="source_metadata")
    if not isinstance(normalized, dict):  # 防御性处理：映射类型会被规范化为字典
        raise TypeError("source_metadata must be a mapping")
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def normalized_source_metadata(source_metadata: Mapping[str, Any]) -> dict[str, Any]:
    """返回与原对象分离、适合存入 JSON 列的规范化元数据。"""
    return json.loads(canonical_source_metadata(source_metadata))


def segment_content_hash(content: str, source_metadata: Mapping[str, Any]) -> str:
    """计算规范化内容及来源元数据的 SHA-256 哈希。"""
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
    """返回分段版本的 UUIDv5 十六进制标识，保证重试时标识不变。"""
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
