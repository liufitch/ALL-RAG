from __future__ import annotations

import csv
import io
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

from rag_modules.parsing.base import ParseContext
from rag_modules.parsing.models import DocumentParseError, ParsedDocument
from rag_modules.parsing.tabular import TableRowBudget, table_blocks
from rag_modules.parsing.text_parser import decode_text


class CsvParser:
    """以保守的解码策略读取分隔文本，并使用确定性的分隔格式回退策略。"""

    source_type = "csv"

    def parse(self, stream: BinaryIO, context: ParseContext) -> ParsedDocument:
        if Path(context.filename).suffix.lower() != ".csv":
            raise DocumentParseError(
                "UNSUPPORTED_FILE_TYPE", "CsvParser only supports .csv files."
            )
        try:
            text, encoding = decode_text(stream.read())
        except DocumentParseError as error:
            raise DocumentParseError(
                "CSV_ENCODING_UNCERTAIN",
                "The CSV encoding could not be determined reliably.",
            ) from error

        dialect = _dialect_for(text)
        try:
            blocks = table_blocks(_csv_rows(text, dialect), "CSV", TableRowBudget())
        except DocumentParseError:
            raise
        except csv.Error as error:
            raise DocumentParseError("CSV_MALFORMED", "The CSV file is malformed.") from error

        if not blocks:
            raise DocumentParseError(
                "NO_EXTRACTABLE_TEXT", "The document contains no extractable text."
            )
        return ParsedDocument(
            document_id=context.document_id,
            filename=context.filename,
            source_type=self.source_type,
            blocks=tuple(blocks),
            metadata={"encoding": encoding},
        )


def _dialect_for(text: str) -> type[csv.Dialect]:
    delimiters = ",;\t|"
    if not any(delimiter in text for delimiter in delimiters):
        return csv.excel
    try:
        return csv.Sniffer().sniff(text[:8192], delimiters=delimiters)
    except csv.Error:
        return csv.excel


def _csv_rows(text: str, dialect: type[csv.Dialect]) -> Iterator[tuple[int, list[str | None]]]:
    reader = csv.reader(io.StringIO(text, newline=""), dialect=dialect, strict=True)
    for values in reader:
        yield reader.line_num, [value if value != "" else None for value in values]
