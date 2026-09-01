import io

import pytest

from rag_modules.parsing.base import ParseContext
from rag_modules.parsing.models import (
    DocumentParseError,
    ParsedBlock,
    ParsedDocument,
)
from rag_modules.parsing.registry import ParserRegistry


class RecordingParser:
    def __init__(self, blocks=None):
        self.calls = []
        self.blocks = blocks

    def parse(self, stream, context):
        self.calls.append(context)
        assert stream.tell() == 0
        blocks = self.blocks
        if blocks is None:
            blocks = (ParsedBlock("paragraph", "body", {}),)
        return ParsedDocument(
            context.document_id,
            context.filename,
            "markdown",
            tuple(blocks),
            {},
        )


def test_registry_dispatches_normalized_extension_and_resets_stream():
    parser = RecordingParser()
    registry = ParserRegistry({".md": parser})
    context = ParseContext(document_id="doc-1", filename="README.MD")
    stream = io.BytesIO(b"body")
    stream.seek(2)

    parsed = registry.parse(".MD", stream, context)

    assert parsed.document_id == "doc-1"
    assert parser.calls[0].filename == "README.MD"


def test_registry_accepts_extension_without_dot():
    parser = RecordingParser()
    parsed = ParserRegistry({"MD": parser}).parse(
        "MD", io.BytesIO(b"body"), ParseContext("d", "x.md")
    )
    assert parsed.source_type == "markdown"


def test_registry_rejects_unknown_extension_with_stable_code():
    with pytest.raises(DocumentParseError) as error:
        ParserRegistry({}).parse(".doc", io.BytesIO(b"x"), ParseContext("d", "x.doc"))
    assert error.value.code == "UNSUPPORTED_FILE_TYPE"
    assert error.value.retryable is False


def test_registry_rejects_empty_document_with_stable_code():
    parser = RecordingParser(blocks=())
    with pytest.raises(DocumentParseError) as error:
        ParserRegistry({".md": parser}).parse(
            ".md", io.BytesIO(b"x"), ParseContext("d", "x.md")
        )
    assert error.value.code == "NO_EXTRACTABLE_TEXT"


def test_parser_error_is_preserved():
    class FailingParser:
        def parse(self, stream, context):
            raise DocumentParseError("PDF_NO_EXTRACTABLE_TEXT", "no text")

    with pytest.raises(DocumentParseError) as error:
        ParserRegistry({".pdf": FailingParser()}).parse(
            ".pdf", io.BytesIO(b"x"), ParseContext("d", "x.pdf")
        )
    assert error.value.code == "PDF_NO_EXTRACTABLE_TEXT"
