from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, datetime, time
from typing import Any

from rag_modules.config.settings import settings
from rag_modules.parsing.models import DocumentParseError, ParsedBlock


def normalize_table_headers(values: Sequence[Any]) -> list[str]:
    """Return stable, self-describing names for a source header row."""
    headers: list[str] = []
    used: set[str] = set()
    for index, value in enumerate(values, start=1):
        label = cell_to_text(value).strip() if value is not None else ""
        candidate = label or f"列{index}"
        unique = candidate
        suffix = 2
        while unique in used:
            unique = f"{candidate}_{suffix}"
            suffix += 1
        used.add(unique)
        headers.append(unique)
    return headers


def cell_to_text(value: Any) -> str:
    """Normalize scalar spreadsheet cells without losing formula text."""
    if isinstance(value, datetime):
        if value.time() == time.min:
            return value.date().isoformat()
        return value.isoformat()
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def row_has_values(values: Sequence[Any]) -> bool:
    """Identify the first header row without treating whitespace as a header."""
    return any(value is not None and cell_to_text(value).strip() for value in values)


def enforce_column_limit(column_count: int) -> None:
    if column_count > settings.parser.max_columns:
        raise DocumentParseError(
            "TABLE_COLUMN_LIMIT_EXCEEDED",
            "The table exceeds the configured column limit.",
        )


def validate_table_values(values: Sequence[Any]) -> None:
    """Apply table limits while a reader still has only the current row in memory."""
    enforce_column_limit(len(values))
    for value in values:
        if value is not None and len(cell_to_text(value)) > settings.parser.max_cell_characters:
            raise DocumentParseError(
                "TABLE_CELL_CHARACTER_LIMIT_EXCEEDED",
                "A table cell exceeds the configured character limit.",
            )


def format_table_row(headers: Sequence[str], values: Sequence[Any]) -> str:
    """Render a table row as self-describing header/value pairs.

    This deliberately small boundary is shared by document parsers that have
    a first-row header. Missing or blank headers receive a stable positional
    label so source values remain understandable after extraction.
    """
    pairs: list[str] = []
    for index, value in enumerate(values, start=1):
        if value is None:
            continue
        header = headers[index - 1].strip() if index <= len(headers) else ""
        label = header or f"列{index}"
        pairs.append(f"{label}：{cell_to_text(value).strip()}")
    return "；".join(pairs)


def table_blocks(
    rows: Iterable[tuple[int, Sequence[Any]]], sheet: str
) -> list[ParsedBlock]:
    """Convert a bounded source-row iterator into normalized table blocks."""
    headers: list[str] | None = None
    blocks: list[ParsedBlock] = []
    for logical_row, (source_row, values) in enumerate(rows, start=1):
        if logical_row > settings.parser.max_rows:
            raise DocumentParseError(
                "TABLE_ROW_LIMIT_EXCEEDED", "The table exceeds the configured row limit."
            )
        validate_table_values(values)
        if headers is None:
            if row_has_values(values):
                headers = normalize_table_headers(values)
            continue
        text = format_table_row(headers, values)
        if text:
            blocks.append(
                ParsedBlock(
                    "table_row",
                    text,
                    {"sheet": sheet, "row": source_row, "headers": headers},
                )
            )
    return blocks
