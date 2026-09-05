from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, BinaryIO

import xlrd
from xlrd.biffh import XLRDError
from xlrd.compdoc import CompDocError

from rag_modules.config.settings import settings
from rag_modules.parsing.base import ParseContext
from rag_modules.parsing.models import DocumentParseError, ParsedDocument, ParserWarning
from rag_modules.parsing.tabular import TableRowBudget, table_blocks
from rag_modules.parsing.warnings import BoundedWarningCollector, parser_warning_summary


class XlsParser:
    """读取旧版 XLS 工作簿的工作表，并及时释放 xlrd 资源。"""

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
            warnings = BoundedWarningCollector[ParserWarning](
                settings.parser.max_warnings_per_document,
                parser_warning_summary,
            )
            budget = TableRowBudget()
            for sheet_name in workbook.sheet_names():
                worksheet = workbook.sheet_by_name(sheet_name)
                if worksheet.visibility != 0:
                    warnings.add(
                        ParserWarning(
                            "HIDDEN_SHEET_SKIPPED",
                            "A hidden worksheet was skipped.",
                            {"sheet": sheet_name},
                        )
                    )
                    continue
                sheet_blocks = table_blocks(
                    _worksheet_rows(worksheet, workbook.datemode), sheet_name, budget
                )
                if not sheet_blocks:
                    warnings.add(
                        ParserWarning(
                            "EMPTY_SHEET_SKIPPED",
                            "A worksheet without data rows was skipped.",
                            {"sheet": sheet_name},
                        )
                    )
                blocks.extend(sheet_blocks)
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
            warnings=warnings.result(),
        )


def _worksheet_rows(worksheet: Any, datemode: int) -> Iterator[tuple[int, list[Any]]]:
    for source_row in range(worksheet.nrows):
        values: list[Any] = []
        for cell in worksheet.row(source_row):
            value = _cell_value(cell, datemode)
            values.append(value)
        yield source_row + 1, values


def _cell_value(cell: Any, datemode: int) -> Any:
    if cell.ctype in {xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK}:
        return None
    if cell.ctype == xlrd.XL_CELL_BOOLEAN:
        return bool(cell.value)
    if cell.ctype == xlrd.XL_CELL_DATE:
        return xlrd.xldate_as_datetime(cell.value, datemode)
    return cell.value


def _malformed_xls_error() -> DocumentParseError:
    return DocumentParseError("XLS_MALFORMED", "The XLS file is malformed or unreadable.")
