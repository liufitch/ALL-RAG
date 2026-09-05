from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any

from rag_modules.config.settings import settings
from rag_modules.parsing.models import DocumentParseError, ParsedBlock


def normalize_table_headers(values: Sequence[Any]) -> list[str]:
    """为源表头行生成稳定且含义明确的列名。"""
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
    """规范化电子表格的标量单元格，同时保留公式文本。"""
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
    """识别首个表头行，避免将纯空白内容视为表头。"""
    return any(value is not None and cell_to_text(value).strip() for value in values)


def enforce_column_limit(column_count: int) -> None:
    if column_count > settings.parser.max_columns:
        raise DocumentParseError(
            "TABLE_COLUMN_LIMIT_EXCEEDED",
            "The table exceeds the configured column limit.",
        )


def validate_table_values(values: Sequence[Any]) -> None:
    """读取器内存中仅保留当前行时，立即应用表格限制。"""
    enforce_column_limit(len(values))
    for value in values:
        if value is not None and len(cell_to_text(value)) > settings.parser.max_cell_characters:
            raise DocumentParseError(
                "TABLE_CELL_CHARACTER_LIMIT_EXCEEDED",
                "A table cell exceeds the configured character limit.",
            )


@dataclass
class TableRowBudget:
    """统计整份文档中表格所消耗的有效源数据行数。"""

    rows_used: int = 0

    def consume(self) -> None:
        self.rows_used += 1
        if self.rows_used > settings.parser.max_rows:
            raise DocumentParseError(
                "TABLE_ROW_LIMIT_EXCEEDED", "The table exceeds the configured row limit."
            )


def format_table_row(headers: Sequence[str], values: Sequence[Any]) -> str:
    """将表格行渲染为带有字段说明的表头与值配对。

    这个职责单一的转换函数由首行为表头的文档解析器共用。
    缺失或空白表头使用稳定的位置标签，使提取后的源数据仍然含义明确。
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
    """在文档总行数预算内完成有限收集后，规范化单个表格。"""
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
    """保留用于对齐的空值间隔，去掉末尾未使用的源列。"""
    last_value = len(values)
    while last_value and values[last_value - 1] is None:
        last_value -= 1
    return list(values[:last_value])
