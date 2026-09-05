"""对解析结果执行确定性分段，供预览和索引流程共用。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Iterator

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


@dataclass
class _WorkBudget:
    remaining_records: int
    remaining_boundary_scan_characters: int

    def ensure_projected_records(self, length: int, maximum: int, overlap: int) -> None:
        hard_advance = maximum - overlap
        minimum_advance = max(1, (hard_advance + 1) // 2)
        projected = (
            1
            if length <= maximum
            else 1 + (length - maximum + minimum_advance - 1) // minimum_advance
        )
        if projected > self.remaining_records:
            self._raise_limit("segment")

    def consume_record(self) -> None:
        if self.remaining_records <= 0:
            self._raise_limit("segment")
        self.remaining_records -= 1

    def consume_boundary_scan(self, characters: int) -> None:
        if characters > self.remaining_boundary_scan_characters:
            self._raise_limit("boundary scan")
        self.remaining_boundary_scan_characters -= characters

    @staticmethod
    def _raise_limit(label: str) -> None:
        raise SegmentationConfigError(
            f"Segmentation exceeded the configured {label} budget.",
            code="SEGMENTATION_LIMIT_EXCEEDED",
        )


@dataclass
class _DelimiterFidelity:
    omitted_count: int = 0


class Segmenter:
    """根据 ``ParsedDocument`` 创建结果确定的普通分段或父子分段。"""

    max_segments = 10_000
    max_source_blocks = 100_000
    max_source_characters = 50 * 1024 * 1024
    max_boundary_scan_characters = 100_000_000

    def __init__(
        self,
        max_segments: int = 10_000,
        max_source_blocks: int = 100_000,
        max_source_characters: int = 50 * 1024 * 1024,
        max_boundary_scan_characters: int = 100_000_000,
    ) -> None:
        if min(
            max_segments,
            max_source_blocks,
            max_source_characters,
            max_boundary_scan_characters,
        ) <= 0:
            raise ValueError("segmentation budgets must be positive")
        self.max_segments = max_segments
        self.max_source_blocks = max_source_blocks
        self.max_source_characters = max_source_characters
        self.max_boundary_scan_characters = max_boundary_scan_characters

    def segment(self, parsed: ParsedDocument, config: SegmentationConfig) -> SegmentationResult:
        _validate_config(config)
        self._validate_source_budget(parsed.blocks)
        blocks = tuple(block for block in parsed.blocks if block.text.strip())
        if not blocks:
            return SegmentationResult(())
        budget = _WorkBudget(self.max_segments, self.max_boundary_scan_characters)
        if isinstance(config, GeneralSegmentationConfig):
            return self._general(blocks, config, budget)
        if isinstance(config, ParentChildSegmentationConfig):
            return self._parent_child(parsed, blocks, config, budget)
        raise SegmentationConfigError("segmentation config must be a supported discriminated config")

    def _general(
        self,
        blocks: tuple[ParsedBlock, ...],
        config: GeneralSegmentationConfig,
        budget: _WorkBudget,
    ) -> SegmentationResult:
        segments: list[PreviewSegment] = []
        serial = 1
        for source in _general_sources(blocks):
            for start, end in _split_ranges(
                source.text,
                config.max_chunk_length,
                config.overlap,
                config.separator,
                budget,
            ):
                content = source.text[start:end]
                if not content.strip():
                    continue
                budget.consume_record()
                segments.append(
                    PreviewSegment(
                        local_id=f"s-{serial:06d}",
                        parent_local_id=None,
                        position=len(segments),
                        content=content,
                        source_metadata=source.metadata_for(start, end),
                        index_type="general",
                    )
                )
                serial += 1
        return SegmentationResult(tuple(segments))

    def _validate_source_budget(self, blocks: tuple[ParsedBlock, ...]) -> None:
        if len(blocks) > self.max_source_blocks:
            raise SegmentationConfigError(
                "Segmentation exceeded the configured source block budget.",
                code="SEGMENTATION_LIMIT_EXCEEDED",
            )
        characters = 0
        for block in blocks:
            characters += len(block.text)
            if characters > self.max_source_characters:
                raise SegmentationConfigError(
                    "Segmentation exceeded the configured source character budget.",
                    code="SEGMENTATION_LIMIT_EXCEEDED",
                )

    def _parent_child(
        self,
        parsed: ParsedDocument,
        blocks: tuple[ParsedBlock, ...],
        config: ParentChildSegmentationConfig,
        budget: _WorkBudget,
    ) -> SegmentationResult:
        spreadsheet = _is_spreadsheet(parsed, blocks)
        warnings: list[ParserWarning] = []
        fallback_fidelity: _DelimiterFidelity | None = None
        parent_sources_are_split = False
        if spreadsheet:
            parent_sources = _spreadsheet_parent_sources(blocks)
        elif config.parent_mode == "full_document":
            parent_sources = (_combine_blocks(blocks),)
            if len(parent_sources[0].text) > config.parent_max_length:
                fallback_fidelity = _DelimiterFidelity()
                parent_sources = _fallback_parent_sources(
                    blocks,
                    config.parent_max_length,
                    config.separator,
                    budget,
                    fallback_fidelity,
                )
                parent_sources_are_split = True
                warnings.append(
                    ParserWarning(
                        "PARENT_FULL_DOCUMENT_FALLBACK",
                        "The full-document parent exceeded its maximum and was split.",
                        {"parent_max_length": config.parent_max_length},
                    )
                )
        else:
            parent_sources = tuple(_source_for_block(block) for block in blocks)

        output: list[PreviewSegment] = []
        parent_serial = 1
        child_serial = 1
        for source in parent_sources:
            # 段落和表格形成的父分段自身也可能超过父分段长度上限。
            parent_parts = (
                (source,)
                if parent_sources_are_split
                else _split_source(
                    source, config.parent_max_length, 0, config.separator, budget
                )
            )
            for parent_part in parent_parts:
                if not parent_part.text.strip():
                    continue
                budget.consume_record()
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
                    budget,
                    atomic_rows=spreadsheet,
                ):
                    content = parent_part.text[start:end]
                    if not content.strip():
                        continue
                    budget.consume_record()
                    output.append(
                        PreviewSegment(
                            local_id=f"c-{child_serial:06d}",
                            parent_local_id=parent_id,
                            position=len(output),
                            content=content,
                            source_metadata=parent_part.metadata_for(start, end),
                            index_type="child",
                        )
                    )
                    child_serial += 1
        if fallback_fidelity and fallback_fidelity.omitted_count:
            warnings.append(
                ParserWarning(
                    "SEGMENT_DELIMITER_OMITTED",
                    "A source delimiter could not be retained within the configured parent length.",
                    {
                        "delimiter": "\\n\\n",
                        "count": fallback_fidelity.omitted_count,
                    },
                )
            )
        return SegmentationResult(tuple(output), tuple(warnings))


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
    """保持代码块和表格记录完整，同时允许合并普通文本的来源范围。"""
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
    blocks: tuple[ParsedBlock, ...],
    maximum: int,
    separator: str | None,
    budget: _WorkBudget,
    fidelity: _DelimiterFidelity,
) -> Iterator[_SourceText]:
    """以流式方式生成长度受限的兜底父分段，同时保持长度合规的原子块完整。"""
    protected = tuple(_is_fitting_atomic(block, maximum) for block in blocks)
    prefixes, suffixes, omitted_count = _allocate_delimiters(
        blocks, protected, maximum
    )
    fidelity.omitted_count += omitted_count

    for index, block in enumerate(blocks):
        if protected[index]:
            yield _source_for_block(block)
            continue
        yield from _split_fallback_block(
            block,
            prefixes[index],
            suffixes[index],
            maximum,
            separator,
            budget,
        )


def _is_fitting_atomic(block: ParsedBlock, maximum: int) -> bool:
    return block.block_type in {"code", "table_row"} and len(block.text) <= maximum


def _allocate_delimiters(
    blocks: tuple[ParsedBlock, ...],
    protected: tuple[bool, ...],
    maximum: int,
) -> tuple[list[int], list[int], int]:
    """对每个源块使用三种有限状态，选择所有边界分隔符的附着方式。"""
    decisions: list[list[int | None]] = [[None, None, None] for _ in blocks]
    next_costs: list[tuple[int, int, int] | None] = [None, None, None]

    for index in range(len(blocks) - 1, -1, -1):
        current_costs: list[tuple[int, int, int] | None] = [None, None, None]
        for prefix_length in range(3):
            if index == len(blocks) - 1:
                split_cost = _delimiter_split_cost(
                    blocks[index], protected[index], prefix_length, 0, maximum
                )
                if split_cost is not None:
                    current_costs[prefix_length] = (0, split_cost, 0)
                continue

            candidates: list[tuple[tuple[int, int, int], int]] = []
            for suffix_length in range(3):
                split_cost = _delimiter_split_cost(
                    blocks[index],
                    protected[index],
                    prefix_length,
                    suffix_length,
                    maximum,
                )
                future = next_costs[2 - suffix_length]
                if split_cost is None or future is None:
                    continue
                preference = _delimiter_preference(
                    blocks,
                    protected,
                    index,
                    prefix_length,
                    suffix_length,
                    maximum,
                )
                candidates.append(
                    (
                        (
                            future[0],
                            split_cost + future[1],
                            preference + future[2],
                        ),
                        suffix_length,
                    )
                )

            split_cost = _delimiter_split_cost(
                blocks[index], protected[index], prefix_length, 0, maximum
            )
            omitted_future = next_costs[0]
            if split_cost is not None and omitted_future is not None:
                candidates.append(
                    (
                        (
                            1 + omitted_future[0],
                            split_cost + omitted_future[1],
                            omitted_future[2],
                        ),
                        -1,
                    )
                )

            if candidates:
                cost, decision = min(candidates, key=lambda item: (item[0], item[1]))
                current_costs[prefix_length] = cost
                decisions[index][prefix_length] = decision
        next_costs = current_costs

    prefixes = [0] * len(blocks)
    suffixes = [0] * len(blocks)
    omitted_count = 0
    prefix_length = 0
    for index in range(len(blocks) - 1):
        decision = decisions[index][prefix_length]
        if decision == -1:
            omitted_count += 1
            prefix_length = 0
            continue
        if decision is None:  # pragma: no cover - 省略全部分隔符的路径始终可行
            raise RuntimeError("delimiter allocation did not find a bounded path")
        suffixes[index] = decision
        prefix_length = 2 - decision
        prefixes[index + 1] = prefix_length
    return prefixes, suffixes, omitted_count


def _delimiter_split_cost(
    block: ParsedBlock,
    protected: bool,
    prefix_length: int,
    suffix_length: int,
    maximum: int,
) -> int | None:
    if protected:
        return 0 if prefix_length == suffix_length == 0 else None
    if not _can_host_delimiters(block, prefix_length, suffix_length, maximum):
        return None
    base_parts = (len(block.text) + maximum - 1) // maximum
    attached_parts = (
        len(block.text) + prefix_length + suffix_length + maximum - 1
    ) // maximum
    return max(0, attached_parts - base_parts)


def _delimiter_preference(
    blocks: tuple[ParsedBlock, ...],
    protected: tuple[bool, ...],
    index: int,
    prefix_length: int,
    suffix_length: int,
    maximum: int,
) -> int:
    if suffix_length == 2 and _fits_delimiters_without_splitting(
        blocks[index], prefix_length, suffix_length, maximum
    ):
        return 0
    if (
        suffix_length == 0
        and not protected[index + 1]
        and _fits_delimiters_without_splitting(
            blocks[index + 1], 2, 0, maximum
        )
    ):
        return 1
    if suffix_length == 2:
        return 2
    if suffix_length == 0:
        return 3
    return 4


def _can_host_delimiters(
    block: ParsedBlock, prefix_length: int, suffix_length: int, maximum: int
) -> bool:
    first = next(
        (index for index, character in enumerate(block.text) if not character.isspace()),
        None,
    )
    if first is None:
        return False
    last = next(
        (
            index
            for index in range(len(block.text) - 1, -1, -1)
            if not block.text[index].isspace()
        ),
        first,
    )
    if prefix_length and prefix_length + first + 1 > maximum:
        return False
    if suffix_length and len(block.text) - last + suffix_length > maximum:
        return False
    if prefix_length and suffix_length and first == last:
        return len(block.text) + prefix_length + suffix_length <= maximum
    return True


def _fits_delimiters_without_splitting(
    block: ParsedBlock, prefix_length: int, suffix_length: int, maximum: int
) -> bool:
    return len(block.text) + prefix_length + suffix_length <= maximum


def _split_fallback_block(
    block: ParsedBlock,
    prefix_length: int,
    suffix_length: int,
    maximum: int,
    separator: str | None,
    budget: _WorkBudget,
) -> Iterator[_SourceText]:
    """仅拆分必要的文本，使附着的分隔符保留在非空白父分段中。"""
    delimiter = "\n\n"
    prefix = delimiter[:prefix_length]
    suffix = delimiter[2 - suffix_length :] if suffix_length else ""
    source = _source_for_block(block)
    if len(source.text) + len(prefix) + len(suffix) <= maximum:
        yield _source_slice(
            source,
            0,
            len(source.text),
            prefix,
            suffix,
        )
        return

    first = next(
        index
        for index, character in enumerate(source.text)
        if not character.isspace()
    )
    last = next(
        index
        for index in range(len(source.text) - 1, -1, -1)
        if not source.text[index].isspace()
    )
    prefix_end = first + 1 if prefix_length else 0
    suffix_start = last if suffix_length else len(source.text)

    if prefix_length:
        prefix_end = min(
            suffix_start,
            max(prefix_end, min(len(source.text), maximum - len(prefix))),
        )
        yield _source_slice(source, 0, prefix_end, prefix, "")
    if prefix_end < suffix_start:
        middle = _source_slice(source, prefix_end, suffix_start)
        yield from _split_source(middle, maximum, 0, separator, budget)
    if suffix_length:
        yield _source_slice(source, suffix_start, len(source.text), "", suffix)


def _source_slice(
    source: _SourceText,
    start: int,
    end: int,
    prefix: str = "",
    suffix: str = "",
) -> _SourceText:
    pieces: list[_SourcePiece] = []
    for piece in source.pieces:
        left, right = max(start, piece.start), min(end, piece.end)
        if left < right:
            pieces.append(
                _SourcePiece(
                    len(prefix) + left - start,
                    len(prefix) + right - start,
                    piece.metadata,
                )
            )
    return _SourceText(prefix + source.text[start:end] + suffix, tuple(pieces))


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


def _split_source(
    source: _SourceText,
    maximum: int,
    overlap: int,
    separator: str | None,
    budget: _WorkBudget,
) -> Iterator[_SourceText]:
    for start, end in _split_ranges(source.text, maximum, overlap, separator, budget):
        intersecting: list[_SourcePiece] = []
        for piece in source.pieces:
            left, right = max(start, piece.start), min(end, piece.end)
            if left < right:
                intersecting.append(_SourcePiece(left - start, right - start, piece.metadata))
        yield _SourceText(source.text[start:end], tuple(intersecting))


def _split_ranges(
    text: str,
    maximum: int,
    overlap: int,
    separator: str | None,
    budget: _WorkBudget,
) -> Iterator[tuple[int, int]]:
    """按 Unicode 字符数拆分，并将选中的边界分隔符保留在分段内。"""
    if not text:
        return
    budget.ensure_projected_records(len(text), maximum, overlap)
    hard_advance = maximum - overlap
    minimum_advance = max(1, (hard_advance + 1) // 2)
    start = 0
    while start < len(text):
        limit = min(start + maximum, len(text))
        if limit == len(text):
            end = limit
        else:
            boundary_end = _boundary_end(text, start, limit, separator, budget)
            end = (
                boundary_end
                if boundary_end
                and boundary_end - overlap - start >= minimum_advance
                else limit
            )
        yield start, end
        if end == len(text):
            break
        next_start = end - overlap
        if next_start <= start:
            raise RuntimeError("segmentation split did not make progress")
        start = next_start


def _child_ranges(
    source: _SourceText,
    maximum: int,
    overlap: int,
    separator: str | None,
    budget: _WorkBudget,
    *,
    atomic_rows: bool,
) -> Iterator[tuple[int, int]]:
    """保持每行电子表格数据具有完整的字段说明，除非该行超过长度上限。"""
    if not atomic_rows:
        yield from _split_ranges(source.text, maximum, overlap, separator, budget)
        return
    for piece in source.pieces:
        for start, end in _split_ranges(
            source.text[piece.start : piece.end], maximum, overlap, separator, budget
        ):
            yield piece.start + start, piece.start + end


def _boundary_end(
    text: str,
    start: int,
    limit: int,
    separator: str | None,
    budget: _WorkBudget,
) -> int | None:
    boundary_sets: tuple[str, ...] = (
        (separator,) if separator else (),
        ("\n\n",),
        ("\n",),
        ("。", "！", "？", "；"),
        (".", "!", "?", ";"),
        (" ", "\t"),
    )
    for choices in boundary_sets:
        best = -1
        for choice in choices:
            budget.consume_boundary_scan(limit - start)
            index = text.rfind(choice, start, limit)
            candidate = index + len(choice)
            if (
                index >= 0
                and candidate > best
                and _contains_non_whitespace(text, start, candidate)
            ):
                best = candidate
        if best >= start:
            return best
    return None


def _contains_non_whitespace(text: str, start: int, end: int) -> bool:
    return any(not text[index].isspace() for index in range(start, end))


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
