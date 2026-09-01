from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO
from zipfile import BadZipFile

from docx import Document
from docx.document import Document as DocxDocument
from docx.opc.exceptions import OpcError
from docx.oxml.exceptions import InvalidXmlError
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph
from lxml.etree import XMLSyntaxError

from rag_modules.parsing.base import ParseContext
from rag_modules.parsing.models import DocumentParseError, ParsedBlock, ParsedDocument
from rag_modules.parsing.tabular import format_table_row
from rag_modules.parsing.text_parser import normalize_text


class DocxParser:
    """Extract body paragraphs and tables from non-executed DOCX content."""

    source_type = "docx"

    def parse(self, stream: BinaryIO, context: ParseContext) -> ParsedDocument:
        if Path(context.filename).suffix.lower() != ".docx":
            raise DocumentParseError(
                "UNSUPPORTED_FILE_TYPE", "DocxParser only supports .docx files."
            )
        try:
            document = Document(stream)
        except DocumentParseError:
            raise
        except (
            BadZipFile,
            OSError,
            ValueError,
            KeyError,
            OpcError,
            InvalidXmlError,
            XMLSyntaxError,
        ) as error:
            raise _malformed_docx_error() from error

        try:
            blocks = _body_blocks(document)
        except DocumentParseError:
            raise
        except (OpcError, InvalidXmlError, XMLSyntaxError) as error:
            raise _malformed_docx_error() from error

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


def _malformed_docx_error() -> DocumentParseError:
    return DocumentParseError("DOCX_MALFORMED", "The DOCX file is malformed or unreadable.")


def _body_blocks(document: DocxDocument) -> list[ParsedBlock]:
    blocks: list[ParsedBlock] = []
    heading_stack: list[tuple[int, str]] = []
    for element in _body_elements(document):
        if isinstance(element, Paragraph):
            text = normalize_text(element.text).strip()
            if not text:
                continue
            level = _heading_level(element)
            if level is not None:
                heading_stack[:] = [entry for entry in heading_stack if entry[0] < level]
                heading_stack.append((level, text))
                block_type = "heading"
            else:
                block_type = "list_item" if _is_list_item(element) else "paragraph"
            blocks.append(
                ParsedBlock(
                    block_type,
                    text,
                    {"heading_path": [title for _, title in heading_stack]},
                )
            )
            continue

        headers, rows = _table_rows(element)
        for values in rows:
            formatted = format_table_row(headers, values)
            if formatted:
                blocks.append(
                    ParsedBlock(
                        "table_row",
                        formatted,
                        {"heading_path": [title for _, title in heading_stack]},
                    )
                )
    return blocks


def _body_elements(document: DocxDocument) -> Iterator[Paragraph | Table]:
    for child in document.element.body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, document.part)
        elif child.tag == qn("w:tbl"):
            yield Table(child, document.part)


def _heading_level(paragraph: Paragraph) -> int | None:
    match = re.fullmatch(r"Heading ([1-6])", paragraph.style.name or "")
    return int(match.group(1)) if match else None


def _is_list_item(paragraph: Paragraph) -> bool:
    return (paragraph.style.name or "").startswith("List ")


def _table_rows(table: Table) -> tuple[list[str], list[list[str]]]:
    rows = [
        [_table_cell_text(cell.text) for cell in row.cells]
        for row in table.rows
    ]
    if not rows:
        return [], []
    return rows[0], rows[1:]


def _table_cell_text(text: str) -> str:
    return " ".join(normalize_text(text).split())
