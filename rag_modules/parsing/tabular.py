from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
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


@dataclass
class TableRowBudget:
    """Document-wide count of meaningful source rows consumed by tables."""

    rows_used: int = 0

    def consume(self) -> None:
        self.rows_used += 1
        if self.rows_used > settings.parser.max_rows:
            raise DocumentParseError(
                "TABLE_ROW_LIMIT_EXCEEDED", "The table exceeds the configured row limit."
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
    rows: Iterable[tuple[int, Sequence[Any]]], sheet: str, budget: TableRowBudget
) -> list[ParsedBlock]:
    """Normalize one table after bounded collection under a document row budget."""
    meaningful_rows: list[tuple[int, list[Any]]] = []
    for source_row, source_values in rows:
        values = _trim_trailing_nulls(source_values)
        if not row_has_values(values):
            continue
        budget.consume()
        validate_table_values(values)
        meaningful_rows.append((source_row, values))

    if not meaningful_rows:
        return []

    header_values = meaningful_rows[0][1]
    final_width = max(len(values) for _, values in meaningful_rows)
    headers = normalize_table_headers(
        [
            header_values[index] if index < len(header_values) else None
            for index in range(final_width)
        ]
    )
    blocks: list[ParsedBlock] = []
    for source_row, values in meaningful_rows[1:]:
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


def _trim_trailing_nulls(values: Sequence[Any]) -> list[Any]:
    """Keep null gaps for alignment but discard unused trailing source columns."""
    last_value = len(values)
    while last_value and values[last_value - 1] is None:
        last_value -= 1
    return list(values[:last_value])
