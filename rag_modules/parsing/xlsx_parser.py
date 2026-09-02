from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, BinaryIO
from zipfile import BadZipFile

from lxml.etree import XMLSyntaxError
from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

from rag_modules.parsing.base import ParseContext
from rag_modules.parsing.models import (
    DocumentParseError,
    ParsedDocument,
    ParserWarning,
)
from rag_modules.parsing.tabular import TableRowBudget, table_blocks


class XlsxParser:
    """Read visible XLSX sheets without evaluating formulas."""

    source_type = "xlsx"

    def parse(self, stream: BinaryIO, context: ParseContext) -> ParsedDocument:
        if Path(context.filename).suffix.lower() != ".xlsx":
            raise DocumentParseError(
                "UNSUPPORTED_FILE_TYPE", "XlsxParser only supports .xlsx files."
            )
        try:
            workbook = load_workbook(stream, read_only=True, data_only=False)
        except (
            BadZipFile,
            InvalidFileException,
            OSError,
            KeyError,
            ValueError,
            XMLSyntaxError,
        ) as error:
            raise _malformed_xlsx_error() from error

        try:
            blocks = []
            warnings = []
            budget = TableRowBudget()
            for worksheet in workbook.worksheets:
                if worksheet.sheet_state != "visible":
                    warnings.append(
                        ParserWarning(
                            "HIDDEN_SHEET_SKIPPED",
                            "A hidden worksheet was skipped.",
                            {"sheet": worksheet.title},
                        )
                    )
                    continue
                blocks.extend(
                    table_blocks(_worksheet_rows(worksheet), worksheet.title, budget)
                )
        except DocumentParseError:
            raise
        except (BadZipFile, OSError, KeyError, ValueError, XMLSyntaxError) as error:
            raise _malformed_xlsx_error() from error
        finally:
            workbook.close()

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
            warnings=tuple(warnings),
        )


def _worksheet_rows(worksheet: Any) -> Iterator[tuple[int, tuple[Any, ...]]]:
    for row in worksheet.iter_rows():
        source_cell = next((cell for cell in row if hasattr(cell, "row")), None)
        if source_cell is None:
            continue
        yield source_cell.row, tuple(cell.value for cell in row)


def _malformed_xlsx_error() -> DocumentParseError:
    return DocumentParseError("XLSX_MALFORMED", "The XLSX file is malformed or unreadable.")
