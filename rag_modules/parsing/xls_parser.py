from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, BinaryIO

import xlrd
from xlrd.biffh import XLRDError
from xlrd.compdoc import CompDocError

from rag_modules.config.settings import settings
from rag_modules.parsing.base import ParseContext
from rag_modules.parsing.models import DocumentParseError, ParsedDocument
from rag_modules.parsing.tabular import enforce_column_limit, table_blocks


class XlsParser:
    """Read legacy XLS workbook sheets while releasing xlrd resources promptly."""

    source_type = "xls"

    def parse(self, stream: BinaryIO, context: ParseContext) -> ParsedDocument:
        if Path(context.filename).suffix.lower() != ".xls":
            raise DocumentParseError(
                "UNSUPPORTED_FILE_TYPE", "XlsParser only supports .xls files."
            )
        try:
            workbook = xlrd.open_workbook(file_contents=stream.read(), on_demand=True)
        except (XLRDError, CompDocError, OSError, ValueError) as error:
            raise _malformed_xls_error() from error

        try:
            blocks = []
            for sheet_name in workbook.sheet_names():
                worksheet = workbook.sheet_by_name(sheet_name)
                if worksheet.nrows > settings.parser.max_rows:
                    raise DocumentParseError(
                        "TABLE_ROW_LIMIT_EXCEEDED",
                        "The table exceeds the configured row limit.",
                    )
                enforce_column_limit(worksheet.ncols)
                blocks.extend(table_blocks(_worksheet_rows(worksheet, workbook.datemode), sheet_name))
        except DocumentParseError:
            raise
        except (XLRDError, CompDocError, OSError, ValueError, IndexError) as error:
            raise _malformed_xls_error() from error
        finally:
            workbook.release_resources()

        if not blocks:
            raise DocumentParseError(
                "NO_EXTRACTABLE_TEXT", "The document contains no extractable text."
            )
        return ParsedDocument(
            document_id=context.document_id,
            filename=context.filename,
            source_type=self.source_type,
            blocks=tuple(blocks),
            metadata={},
        )


def _worksheet_rows(worksheet: Any, datemode: int) -> Iterator[tuple[int, list[Any]]]:
    for source_row in range(worksheet.nrows):
        values: list[Any] = []
        for cell in worksheet.row(source_row):
            value = cell.value
            if cell.ctype == xlrd.XL_CELL_DATE:
                value = xlrd.xldate_as_datetime(value, datemode)
            values.append(value)
        yield source_row + 1, values


def _malformed_xls_error() -> DocumentParseError:
    return DocumentParseError("XLS_MALFORMED", "The XLS file is malformed or unreadable.")
