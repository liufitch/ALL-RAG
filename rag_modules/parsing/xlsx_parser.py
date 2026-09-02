from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path
from typing import Any, BinaryIO
from zipfile import BadZipFile, ZipFile

from lxml import etree
from lxml.etree import XMLSyntaxError
from openpyxl import load_workbook
from openpyxl.utils.cell import coordinate_to_tuple
from openpyxl.utils.exceptions import InvalidFileException

from rag_modules.config.settings import settings
from rag_modules.parsing.base import ParseContext
from rag_modules.parsing.models import DocumentParseError, ParsedDocument, ParserWarning
from rag_modules.parsing.tabular import TableRowBudget, table_blocks


class XlsxParser:
    """Read visible XLSX sheets without evaluating formulas or expanding dimensions."""

    source_type = "xlsx"

    def parse(self, stream: BinaryIO, context: ParseContext) -> ParsedDocument:
        if Path(context.filename).suffix.lower() != ".xlsx":
            raise DocumentParseError(
                "UNSUPPORTED_FILE_TYPE", "XlsxParser only supports .xlsx files."
            )
        payload = stream.read()
        try:
            _preflight_worksheet_xml(payload)
            formula_workbook = load_workbook(
                io.BytesIO(payload), read_only=False, data_only=False, keep_links=False
            )
            cached_workbook = load_workbook(
                io.BytesIO(payload), read_only=False, data_only=True, keep_links=False
            )
        except DocumentParseError:
            raise
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
            warnings: list[ParserWarning] = []
            budget = TableRowBudget()
            cached_by_title = {sheet.title: sheet for sheet in cached_workbook.worksheets}
            for worksheet in formula_workbook.worksheets:
                if worksheet.sheet_state != "visible":
                    warnings.append(_hidden_sheet_warning(worksheet.title))
                    continue
                sheet_blocks = table_blocks(
                    _worksheet_rows(
                        worksheet,
                        cached_by_title[worksheet.title],
                        warnings,
                    ),
                    worksheet.title,
                    budget,
                )
                if not sheet_blocks:
                    warnings.append(_empty_sheet_warning(worksheet.title))
                blocks.extend(sheet_blocks)
        except DocumentParseError:
            raise
        except (BadZipFile, OSError, KeyError, ValueError, XMLSyntaxError) as error:
            raise _malformed_xlsx_error() from error
        finally:
            formula_workbook.close()
            cached_workbook.close()

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


def _preflight_worksheet_xml(payload: bytes) -> None:
    """Bound physical OOXML work before openpyxl constructs any worksheet cells."""
    nodes = 0
    cells = 0
    try:
        with ZipFile(io.BytesIO(payload)) as archive:
            worksheet_names = sorted(
                name
                for name in archive.namelist()
                if name.startswith("xl/worksheets/") and name.endswith(".xml")
            )
            for name in worksheet_names:
                with archive.open(name) as worksheet_xml:
                    for _event, element in etree.iterparse(
                        worksheet_xml,
                        events=("end",),
                        resolve_entities=False,
                        no_network=True,
                        huge_tree=False,
                    ):
                        nodes += 1
                        if nodes > settings.parser.max_spreadsheet_xml_nodes:
                            raise DocumentParseError(
                                "TABLE_XML_NODE_LIMIT_EXCEEDED",
                                "The spreadsheet exceeds the configured XML node limit.",
                            )
                        localname = etree.QName(element).localname
                        if localname == "c":
                            cells += 1
                            if cells > settings.parser.max_physical_cells:
                                raise DocumentParseError(
                                    "TABLE_PHYSICAL_CELL_LIMIT_EXCEEDED",
                                    "The spreadsheet exceeds the configured physical cell limit.",
                                )
                            _validate_physical_cell_coordinate(element)
                        if localname not in {"f", "v", "t", "is"}:
                            element.clear()
    except DocumentParseError:
        raise
    except (BadZipFile, KeyError, OSError, ValueError, XMLSyntaxError) as error:
        raise _malformed_xlsx_error() from error


def _validate_physical_cell_coordinate(element: Any) -> None:
    coordinate = element.get("r")
    if not coordinate or not _physical_cell_has_content(element):
        return
    try:
        row, column = coordinate_to_tuple(coordinate)
    except (TypeError, ValueError) as error:
        raise _malformed_xlsx_error() from error
    if row > settings.parser.max_row_coordinate:
        raise DocumentParseError(
            "TABLE_ROW_LIMIT_EXCEEDED",
            "A non-empty spreadsheet cell exceeds the configured row coordinate limit.",
        )
    if column > min(settings.parser.max_columns, settings.parser.max_column_coordinate):
        raise DocumentParseError(
            "TABLE_COLUMN_LIMIT_EXCEEDED",
            "A non-empty spreadsheet cell exceeds the configured column limit.",
        )


def _physical_cell_has_content(element: Any) -> bool:
    for child in element.iterdescendants():
        localname = etree.QName(child).localname
        if localname == "f":
            return True
        if localname in {"v", "t"} and (child.text or "") != "":
            return True
    return False


def _worksheet_rows(
    worksheet: Any,
    cached_worksheet: Any,
    warnings: list[ParserWarning],
) -> Iterator[tuple[int, tuple[Any, ...]]]:
    """Yield sparse physical rows, never the rectangular declared dimension."""
    rows: dict[int, dict[int, Any]] = {}
    for (source_row, source_column), cell in worksheet._cells.items():
        value = cell.value
        if cell.data_type == "f":
            cached = cached_worksheet.cell(source_row, source_column).value
            if cached is not None:
                value = cached
            else:
                value = value if str(value).startswith("=") else f"={value}"
                warnings.append(
                    ParserWarning(
                        "FORMULA_CACHE_UNAVAILABLE",
                        "A formula had no cached value; its formula text was retained.",
                        {"sheet": worksheet.title, "cell": cell.coordinate},
                    )
                )
        if value is not None:
            rows.setdefault(source_row, {})[source_column] = value
    for source_row in sorted(rows):
        columns = rows[source_row]
        last_column = max(columns)
        yield source_row, tuple(columns.get(column) for column in range(1, last_column + 1))


def _hidden_sheet_warning(sheet: str) -> ParserWarning:
    return ParserWarning(
        "HIDDEN_SHEET_SKIPPED",
        "A hidden worksheet was skipped.",
        {"sheet": sheet},
    )


def _empty_sheet_warning(sheet: str) -> ParserWarning:
    return ParserWarning(
        "EMPTY_SHEET_SKIPPED",
        "A worksheet without data rows was skipped.",
        {"sheet": sheet},
    )


def _malformed_xlsx_error() -> DocumentParseError:
    return DocumentParseError("XLSX_MALFORMED", "The XLSX file is malformed or unreadable.")
