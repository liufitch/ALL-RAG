from __future__ import annotations

from datetime import date, datetime
import math
from typing import Any, BinaryIO

import yaml
from markdown_it import MarkdownIt
from yaml.events import AliasEvent, CollectionEndEvent, CollectionStartEvent
from yaml.tokens import AliasToken, AnchorToken

from rag_modules.parsing.base import ParseContext
from rag_modules.parsing.models import DocumentParseError, ParsedBlock, ParsedDocument
from rag_modules.parsing.text_parser import decode_text, normalize_text


class _BoundedSafeLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate keys instead of silently collapsing shape."""

    def construct_mapping(self, node, deep=False):
        mapping: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in mapping
            except TypeError as error:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    "found an unhashable key",
                    key_node.start_mark,
                ) from error
            if duplicate:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    "found a duplicate key",
                    key_node.start_mark,
                )
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


class MarkdownParser:
    source_type = "markdown"

    def __init__(self) -> None:
        self._markdown = MarkdownIt("commonmark", {"html": False}).enable("table")

    def parse(self, stream: BinaryIO, context: ParseContext) -> ParsedDocument:
        text, encoding = decode_text(stream.read())
        source, front_matter, line_offset = _extract_front_matter(normalize_text(text))
        blocks, links = _walk_tokens(self._markdown.parse(source), line_offset)
        if not blocks:
            raise DocumentParseError(
                "NO_EXTRACTABLE_TEXT", "The document contains no extractable text."
            )
        metadata: dict[str, Any] = {"encoding": encoding}
        if front_matter is not None:
            metadata.update(front_matter)
        if links:
            metadata["links"] = links
        return ParsedDocument(
            document_id=context.document_id,
            filename=context.filename,
            source_type=self.source_type,
            blocks=tuple(blocks),
            metadata=metadata,
        )


_MAX_FRONT_MATTER_RAW_CHARACTERS = 65_536
_MAX_FRONT_MATTER_SCALAR_CHARACTERS = 16_384
_MAX_FRONT_MATTER_DEPTH = 20
_MAX_FRONT_MATTER_NODES = 10_000


def _preflight_front_matter(raw: str) -> None:
    """Reject unsafe YAML syntax and shape before constructing Python values."""
    depth = 0
    event_count = 0
    for event in yaml.parse(raw, Loader=yaml.SafeLoader):
        event_count += 1
        if event_count > _MAX_FRONT_MATTER_NODES:
            raise ValueError("front matter event count exceeds configured limits")
        if isinstance(event, AliasEvent) or getattr(event, "anchor", None) is not None:
            raise ValueError("YAML aliases and anchors are not supported")
        if isinstance(event, CollectionStartEvent):
            depth += 1
            if depth > _MAX_FRONT_MATTER_DEPTH:
                raise ValueError("front matter depth exceeds configured limits")
        elif isinstance(event, CollectionEndEvent):
            depth -= 1
            if depth < 0:
                raise ValueError("front matter collection depth is invalid")
    if depth != 0:
        raise ValueError("front matter collection depth is unbalanced")


def _extract_front_matter(text: str) -> tuple[str, dict[str, Any] | None, int]:
    lines = text.split("\n")
    if not lines or lines[0] != "---":
        return text, None, 0
    for index in range(1, len(lines)):
        if lines[index] != "---":
            continue
        raw_front_matter = "\n".join(lines[1:index])
        bounded_raw = raw_front_matter[:_MAX_FRONT_MATTER_RAW_CHARACTERS]
        if len(raw_front_matter) > _MAX_FRONT_MATTER_RAW_CHARACTERS:
            return (
                "\n".join(lines[index + 1 :]),
                {"front_matter_raw": bounded_raw},
                index + 1,
            )
        try:
            if any(
                isinstance(token, (AliasToken, AnchorToken))
                for token in yaml.scan(raw_front_matter, Loader=yaml.SafeLoader)
            ):
                raise ValueError("YAML aliases and anchors are not supported")
            _preflight_front_matter(raw_front_matter)
            normalized = _normalize_front_matter(yaml.load(raw_front_matter, Loader=_BoundedSafeLoader))
            return (
                "\n".join(lines[index + 1 :]),
                {"front_matter": normalized},
                index + 1,
            )
        except (RecursionError, TypeError, ValueError, yaml.YAMLError):
            return (
                "\n".join(lines[index + 1 :]),
                {"front_matter_raw": bounded_raw},
                index + 1,
            )
    return text, None, 0


def _normalize_front_matter(value: Any) -> Any:
    """Return bounded, acyclic JSON-compatible metadata or reject the YAML value."""
    nodes = 0
    active_ids: set[int] = set()

    def visit(item: Any, depth: int) -> Any:
        nonlocal nodes
        nodes += 1
        if nodes > _MAX_FRONT_MATTER_NODES or depth > _MAX_FRONT_MATTER_DEPTH:
            raise ValueError("front matter shape exceeds configured limits")
        if item is None or isinstance(item, (bool, int)):
            return item
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError("front matter contains a non-finite number")
            return item
        if isinstance(item, str):
            if len(item) > _MAX_FRONT_MATTER_SCALAR_CHARACTERS:
                raise ValueError("front matter scalar exceeds configured limits")
            return item
        if isinstance(item, (date, datetime)):
            return item.isoformat()
        if not isinstance(item, (dict, list)):
            raise TypeError("front matter contains a non-JSON value")
        identity = id(item)
        if identity in active_ids:
            raise ValueError("front matter contains a cycle")
        active_ids.add(identity)
        try:
            if isinstance(item, list):
                return [visit(child, depth + 1) for child in item]
            normalized: dict[str, Any] = {}
            for key, child in item.items():
                if not isinstance(key, str) or len(key) > _MAX_FRONT_MATTER_SCALAR_CHARACTERS:
                    raise TypeError("front matter keys must be bounded strings")
                normalized[key] = visit(child, depth + 1)
            return normalized
        finally:
            active_ids.remove(identity)

    return visit(value, 0)


def _walk_tokens(tokens, line_offset: int) -> tuple[list[ParsedBlock], list[str]]:
    blocks: list[ParsedBlock] = []
    links: list[str] = []
    heading_stack: list[tuple[int, str]] = []
    list_item_depth = 0
    table_cells: list[str] | None = None
    active_heading_level: int | None = None
    active_paragraph_map: list[int] | None = None

    for token in tokens:
        if token.type == "heading_open":
            active_heading_level = int(token.tag[1:])
            active_paragraph_map = token.map
        elif token.type == "paragraph_open":
            active_paragraph_map = token.map
        elif token.type == "list_item_open":
            list_item_depth += 1
        elif token.type == "list_item_close":
            list_item_depth -= 1
        elif token.type == "tr_open":
            table_cells = []
            active_paragraph_map = token.map
        elif token.type == "tr_close":
            if table_cells is not None:
                _append_block(
                    blocks,
                    "table_row",
                    " | ".join(table_cells),
                    heading_stack,
                    active_paragraph_map,
                    line_offset,
                )
            table_cells = None
        elif token.type == "fence":
            language = token.info.strip().split(maxsplit=1)[0] if token.info.strip() else None
            metadata = _block_metadata(heading_stack, token.map, line_offset)
            if language:
                metadata["language"] = language
            if token.content:
                blocks.append(ParsedBlock("code", token.content, metadata))
        elif token.type == "inline":
            text = _inline_text(token, links)
            if table_cells is not None:
                table_cells.append(text)
            elif active_heading_level is not None:
                heading_stack = [
                    entry for entry in heading_stack if entry[0] < active_heading_level
                ]
                heading_stack.append((active_heading_level, text))
                _append_block(
                    blocks,
                    "heading",
                    text,
                    heading_stack,
                    active_paragraph_map,
                    line_offset,
                )
                active_heading_level = None
            elif text.strip():
                _append_block(
                    blocks,
                    "list_item" if list_item_depth else "paragraph",
                    text,
                    heading_stack,
                    active_paragraph_map,
                    line_offset,
                )

    return blocks, links


def _inline_text(token, links: list[str]) -> str:
    pieces: list[str] = []
    for child in token.children or ():
        if child.type in {"link_open", "image"}:
            destination = child.attrs.get("href") or child.attrs.get("src")
            if destination and destination not in links:
                links.append(destination)
        if child.type in {"softbreak", "hardbreak"}:
            pieces.append("\n")
        elif child.type not in {"link_open", "link_close"}:
            pieces.append(child.content)
    return "".join(pieces)


def _append_block(
    blocks: list[ParsedBlock],
    block_type: str,
    text: str,
    heading_stack: list[tuple[int, str]],
    source_map: list[int] | None,
    line_offset: int,
) -> None:
    if text.strip():
        blocks.append(
            ParsedBlock(
                block_type,
                text,
                _block_metadata(heading_stack, source_map, line_offset),
            )
        )


def _block_metadata(
    heading_stack: list[tuple[int, str]], source_map: list[int] | None, line_offset: int
) -> dict[str, Any]:
    metadata: dict[str, Any] = {"heading_path": [title for _, title in heading_stack]}
    if source_map:
        metadata["line_start"] = source_map[0] + 1 + line_offset
        metadata["line_end"] = source_map[1] + line_offset
    return metadata
