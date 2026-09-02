"""Deterministic segmentation over parser output, shared by preview and indexing work."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from rag_modules.parsing.models import ParsedBlock, ParsedDocument, ParserWarning

from .models import (
    GeneralSegmentationConfig,
    ParentChildSegmentationConfig,
    PreviewSegment,
    SegmentationConfig,
    SegmentationConfigError,
    SegmentationResult,
)


@dataclass(frozen=True)
class _SourcePiece:
    start: int
    end: int
    metadata: dict[str, Any]


@dataclass(frozen=True)
class _SourceText:
    text: str
    pieces: tuple[_SourcePiece, ...]

    def metadata_for(self, start: int, end: int) -> dict[str, Any]:
        return _merge_metadata(
            piece.metadata
            for piece in self.pieces
            if piece.start < end and start < piece.end
        )


class Segmenter:
    """Create deterministic flat or parent-child segments from a ``ParsedDocument``."""

    def segment(self, parsed: ParsedDocument, config: SegmentationConfig) -> SegmentationResult:
        _validate_config(config)
        blocks = tuple(block for block in parsed.blocks if block.text)
        if not blocks:
            return SegmentationResult(())
        if isinstance(config, GeneralSegmentationConfig):
            return self._general(blocks, config)
        if isinstance(config, ParentChildSegmentationConfig):
            return self._parent_child(parsed, blocks, config)
        raise SegmentationConfigError("segmentation config must be a supported discriminated config")

    def _general(
        self, blocks: tuple[ParsedBlock, ...], config: GeneralSegmentationConfig
    ) -> SegmentationResult:
        segments: list[PreviewSegment] = []
        serial = 1
        for source in _general_sources(blocks):
            for start, end in _split_ranges(
                source.text, config.max_chunk_length, config.overlap, config.separator
            ):
                segments.append(
                    PreviewSegment(
                        local_id=f"s-{serial:06d}",
                        parent_local_id=None,
                        position=len(segments),
                        content=source.text[start:end],
                        source_metadata=source.metadata_for(start, end),
                        index_type="general",
                    )
                )
                serial += 1
        return SegmentationResult(tuple(segments))

    def _parent_child(
        self,
        parsed: ParsedDocument,
        blocks: tuple[ParsedBlock, ...],
        config: ParentChildSegmentationConfig,
    ) -> SegmentationResult:
        spreadsheet = _is_spreadsheet(parsed, blocks)
        if spreadsheet:
            parent_sources = _spreadsheet_parent_sources(blocks)
            warning: ParserWarning | None = None
        elif config.parent_mode == "full_document":
            parent_sources = (_combine_blocks(blocks),)
            warning = None
            if len(parent_sources[0].text) > config.parent_max_length:
                parent_sources = _fallback_parent_sources(
                    blocks, config.parent_max_length, config.separator
                )
                warning = ParserWarning(
                    "PARENT_FULL_DOCUMENT_FALLBACK",
                    "The full-document parent exceeded its maximum and was split.",
                    {"parent_max_length": config.parent_max_length},
                )
        else:
            parent_sources = tuple(_source_for_block(block) for block in blocks)
            warning = None

        output: list[PreviewSegment] = []
        parent_serial = 1
        child_serial = 1
        for source in parent_sources:
            # Paragraph and table parents may themselves exceed the hard parent maximum.
            parent_parts = _split_source(source, config.parent_max_length, 0, config.separator)
            for parent_part in parent_parts:
                parent_id = f"p-{parent_serial:06d}"
                parent_serial += 1
                parent = PreviewSegment(
                    local_id=parent_id,
                    parent_local_id=None,
                    position=len(output),
                    content=parent_part.text,
                    source_metadata=parent_part.metadata_for(0, len(parent_part.text)),
                    index_type="parent",
                )
                output.append(parent)
                for start, end in _child_ranges(
                    parent_part,
                    config.child_max_length,
                    config.child_overlap,
                    config.separator,
                    atomic_rows=spreadsheet,
                ):
                    output.append(
                        PreviewSegment(
                            local_id=f"c-{child_serial:06d}",
                            parent_local_id=parent_id,
                            position=len(output),
                            content=parent_part.text[start:end],
                            source_metadata=parent_part.metadata_for(start, end),
                            index_type="child",
                        )
                    )
                    child_serial += 1
        return SegmentationResult(tuple(output), (warning,) if warning else ())


def _validate_config(config: SegmentationConfig) -> None:
    if isinstance(config, GeneralSegmentationConfig):
        _validate_limit(config.max_chunk_length, config.overlap, "general")
        return
    if isinstance(config, ParentChildSegmentationConfig):
        _validate_limit(config.parent_max_length, 0, "parent")
        _validate_limit(config.child_max_length, config.child_overlap, "child")
        if config.parent_mode not in {"paragraph", "full_document"}:
            raise SegmentationConfigError("parent_mode must be 'paragraph' or 'full_document'")
        return
    raise SegmentationConfigError("segmentation config must be a supported discriminated config")


def _validate_limit(maximum: int, overlap: int, label: str) -> None:
    if maximum <= 0 or overlap < 0 or overlap >= maximum:
        raise SegmentationConfigError(
            f"{label} maximum must be positive and overlap must satisfy 0 <= overlap < maximum"
        )


def _general_sources(blocks: tuple[ParsedBlock, ...]) -> tuple[_SourceText, ...]:
    """Keep code/table records atomic while allowing prose source ranges to merge."""
    sources: list[_SourceText] = []
    prose: list[ParsedBlock] = []
    for block in blocks:
        if block.block_type in {"code", "table_row"}:
            if prose:
                sources.append(_combine_blocks(prose))
                prose = []
            sources.append(_source_for_block(block))
        else:
            prose.append(block)
    if prose:
        sources.append(_combine_blocks(prose))
    return tuple(sources)


def _fallback_parent_sources(
    blocks: tuple[ParsedBlock, ...], maximum: int, separator: str | None
) -> tuple[_SourceText, ...]:
    """Degrade a full document without breaking atomic units or losing delimiters."""
    sources: list[_SourceText] = []
    prose: list[ParsedBlock] = []
    previous: ParsedBlock | None = None
    for block in blocks:
        if block.block_type not in {"code", "table_row"}:
            if previous and previous.block_type in {"code", "table_row"}:
                sources.append(_delimiter_source(previous, block))
            prose.append(block)
            previous = block
            continue
        if prose:
            sources.append(_combine_blocks(prose))
            prose = []
        if previous:
            sources.append(_delimiter_source(previous, block))
        sources.append(_source_for_block(block))
        previous = block
    if prose:
        sources.append(_combine_blocks(prose))
    split_sources: list[_SourceText] = []
    for source in sources:
        split_sources.extend(_split_source(source, maximum, 0, separator))
    return tuple(split_sources)


def _delimiter_source(previous: ParsedBlock, current: ParsedBlock) -> _SourceText:
    """Keep an inter-block delimiter traceable while leaving both atomic blocks intact."""
    return _SourceText(
        "\n\n",
        (_SourcePiece(0, 2, _merge_metadata((previous.metadata, current.metadata))),),
    )


def _source_for_block(block: ParsedBlock) -> _SourceText:
    return _SourceText(block.text, (_SourcePiece(0, len(block.text), dict(block.metadata)),))


def _combine_blocks(blocks: Iterable[ParsedBlock], delimiter: str = "\n\n") -> _SourceText:
    text_parts: list[str] = []
    pieces: list[_SourcePiece] = []
    cursor = 0
    for block in blocks:
        if text_parts:
            text_parts.append(delimiter)
            cursor += len(delimiter)
        text_parts.append(block.text)
        pieces.append(_SourcePiece(cursor, cursor + len(block.text), dict(block.metadata)))
        cursor += len(block.text)
    return _SourceText("".join(text_parts), tuple(pieces))


def _split_source(source: _SourceText, maximum: int, overlap: int, separator: str | None) -> tuple[_SourceText, ...]:
    result: list[_SourceText] = []
    for start, end in _split_ranges(source.text, maximum, overlap, separator):
        intersecting: list[_SourcePiece] = []
        for piece in source.pieces:
            left, right = max(start, piece.start), min(end, piece.end)
            if left < right:
                intersecting.append(_SourcePiece(left - start, right - start, piece.metadata))
        result.append(_SourceText(source.text[start:end], tuple(intersecting)))
    return tuple(result)


def _split_ranges(text: str, maximum: int, overlap: int, separator: str | None) -> tuple[tuple[int, int], ...]:
    """Split by Unicode character count, retaining the selected boundary in the chunk."""
    if not text:
        return ()
    ranges: list[tuple[int, int]] = []
    start = 0
    while start < len(text):
        limit = min(start + maximum, len(text))
        if limit == len(text):
            end = limit
        else:
            boundary_end = _boundary_end(text, start, limit, separator)
            end = boundary_end if boundary_end and boundary_end - start > overlap else limit
        ranges.append((start, end))
        if end == len(text):
            break
        next_start = end - overlap
        if next_start <= start:
            raise RuntimeError("segmentation split did not make progress")
        start = next_start
    return tuple(ranges)


def _child_ranges(
    source: _SourceText,
    maximum: int,
    overlap: int,
    separator: str | None,
    *,
    atomic_rows: bool,
) -> tuple[tuple[int, int], ...]:
    """Keep every spreadsheet row self-describing unless it exceeds the hard max."""
    if not atomic_rows:
        return _split_ranges(source.text, maximum, overlap, separator)
    ranges: list[tuple[int, int]] = []
    for piece in source.pieces:
        ranges.extend(
            (piece.start + start, piece.start + end)
            for start, end in _split_ranges(
                source.text[piece.start : piece.end], maximum, overlap, separator
            )
        )
    return tuple(ranges)


def _boundary_end(text: str, start: int, limit: int, separator: str | None) -> int | None:
    boundary_sets: tuple[str, ...] = (
        (separator,) if separator else (),
        ("\n\n",),
        ("\n",),
        ("。", "！", "？", "；"),
        (".", "!", "?", ";"),
        (" ", "\t"),
    )
    window = text[start:limit]
    for choices in boundary_sets:
        best = -1
        for choice in choices:
            index = window.rfind(choice)
            if index >= 0 and index + len(choice) > best:
                best = index + len(choice)
        if best > 0:
            return start + best
    return None


def _merge_metadata(metadata_items: Iterable[dict[str, Any]]) -> dict[str, Any]:
    items = list(metadata_items)
    if not items:
        return {}
    merged: dict[str, Any] = {}
    _merge_range(items, merged, "line_start", "line_end")
    _merge_number(items, merged, "page")
    _merge_number(items, merged, "row")
    for key in ("sheet", "headers", "language"):
        values = [item[key] for item in items if key in item]
        if values:
            merged[key] = values[0] if all(value == values[0] for value in values) else values
    paths = [item["heading_path"] for item in items if "heading_path" in item]
    if paths:
        merged["heading_path"] = paths[0]
        distinct_paths = list(dict.fromkeys(tuple(path) for path in paths))
        if len(distinct_paths) > 1:
            merged["heading_paths"] = [list(path) for path in distinct_paths]
    for key in sorted(set().union(*(item.keys() for item in items))):
        if key in merged or key in {"line_start", "line_end", "page", "row", "heading_path"}:
            continue
        values = [item[key] for item in items if key in item]
        if values and all(value == values[0] for value in values):
            merged[key] = values[0]
    return merged


def _merge_range(
    items: list[dict[str, Any]], target: dict[str, Any], start_key: str, end_key: str
) -> None:
    starts = [item[start_key] for item in items if start_key in item]
    ends = [item.get(end_key, item[start_key]) for item in items if start_key in item]
    if starts:
        target[start_key] = min(starts)
        target[end_key] = max(ends)


def _merge_number(items: list[dict[str, Any]], target: dict[str, Any], key: str) -> None:
    values = [item[key] for item in items if key in item]
    if not values:
        return
    if len(set(values)) == 1:
        target[key] = values[0]
    else:
        target[f"{key}_start"] = min(values)
        target[f"{key}_end"] = max(values)


def _is_spreadsheet(parsed: ParsedDocument, blocks: tuple[ParsedBlock, ...]) -> bool:
    return parsed.source_type in {"csv", "xls", "xlsx"} or all(
        block.block_type == "table_row" and "sheet" in block.metadata for block in blocks
    )


def _spreadsheet_parent_sources(blocks: tuple[ParsedBlock, ...]) -> tuple[_SourceText, ...]:
    groups: list[list[ParsedBlock]] = []
    for block in blocks:
        if not groups or not _same_spreadsheet_row_group(groups[-1][-1], block):
            groups.append([block])
        else:
            groups[-1].append(block)
    return tuple(_combine_blocks(group, "\n") for group in groups)


def _same_spreadsheet_row_group(previous: ParsedBlock, current: ParsedBlock) -> bool:
    if previous.metadata.get("sheet") != current.metadata.get("sheet"):
        return False
    previous_row = previous.metadata.get("row")
    current_row = current.metadata.get("row")
    return (
        isinstance(previous_row, int)
        and isinstance(current_row, int)
        and current_row == previous_row + 1
    )
