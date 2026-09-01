import io

import pytest

from rag_modules.parsing.base import ParseContext
from rag_modules.parsing.markdown_parser import MarkdownParser
from rag_modules.parsing.models import DocumentParseError
from rag_modules.parsing.text_parser import TextParser


def test_text_parser_detects_encoding_and_preserves_source_lines():
    """Removing charset fallback or line tracking must break this behavior."""
    parsed = TextParser().parse(
        io.BytesIO("第一段\n\n第二段".encode("gb18030")),
        ParseContext("doc-1", "guide.txt"),
    )

    assert [block.text for block in parsed.blocks] == ["第一段", "第二段"]
    assert parsed.blocks[1].metadata == {"line_start": 3, "line_end": 3}
    assert parsed.metadata["encoding"] == "gb18030"


def test_text_parser_normalizes_bom_newlines_and_nuls_before_paragraph_split():
    """Removing input normalization must break the emitted paragraphs and ranges."""
    parsed = TextParser().parse(
        io.BytesIO(b"\xef\xbb\xbfFirst\x00 line\r\n\r\nSecond\rthird"),
        ParseContext("doc-1", "notes.txt"),
    )

    assert [block.text for block in parsed.blocks] == ["First line", "Second\nthird"]
    assert [block.metadata for block in parsed.blocks] == [
        {"line_start": 1, "line_end": 1},
        {"line_start": 3, "line_end": 4},
    ]
    assert parsed.metadata["encoding"] == "utf-8-sig"


def test_text_parser_rejects_chaotic_binary_with_stable_encoding_error():
    """Accepting non-text bytes must break the parser boundary contract."""
    with pytest.raises(DocumentParseError) as error:
        TextParser().parse(
            io.BytesIO(bytes(range(256)) * 4),
            ParseContext("doc-1", "blob.txt"),
        )

    assert error.value.code == "TEXT_ENCODING_UNCERTAIN"
    assert error.value.retryable is False


def test_text_parser_rejects_control_heavy_valid_utf8_before_nul_normalization():
    """Skipping quality checks after UTF-8 decoding must admit binary-like text."""
    payload = (b"visible\x00 text\x01\x02\x03\x04" * 12)

    with pytest.raises(DocumentParseError) as error:
        TextParser().parse(io.BytesIO(payload), ParseContext("doc-1", "control.txt"))

    assert error.value.code == "TEXT_ENCODING_UNCERTAIN"


def test_text_parser_accepts_genuine_bomless_utf16_japanese_text():
    """Rejecting BOM-less UTF-16 or selecting a corrupt legacy decode must fail."""
    parsed = TextParser().parse(
        io.BytesIO("日本語のテキスト".encode("utf-16-le")),
        ParseContext("doc-1", "japanese.txt"),
    )

    assert [block.text for block in parsed.blocks] == ["日本語のテキスト"]
    assert parsed.metadata["encoding"] == "utf_16_le"


@pytest.mark.parametrize("source", ["Plain Latin text", "Привет, мир"])
def test_text_parser_accepts_bomless_utf16_text_that_is_valid_utf8_bytes(source):
    """A low-quality UTF-8 decode must fall through to the UTF-16 evidence."""
    parsed = TextParser().parse(
        io.BytesIO(source.encode("utf-16-le")),
        ParseContext("doc-1", "utf16.txt"),
    )

    assert [block.text for block in parsed.blocks] == [source]
    assert parsed.metadata["encoding"] == "utf_16_le"


def test_text_parser_decodes_mixed_language_gb18030_without_script_guessing():
    """Mixed source scripts must not make a legacy decode silently corrupt."""
    source = "Release 2: 中文, café — ready"
    parsed = TextParser().parse(
        io.BytesIO(source.encode("gb18030")),
        ParseContext("doc-1", "mixed.txt"),
    )

    assert [block.text for block in parsed.blocks] == [source]
    assert parsed.metadata["encoding"] == "gb18030"


def test_text_parser_rejects_ambiguous_six_byte_non_utf8_payload():
    """Choosing one equally valid legacy decoding must break this uncertainty boundary."""
    with pytest.raises(DocumentParseError) as error:
        TextParser().parse(
            io.BytesIO(b"\xc0\xc1\xc2\xc3\xc4\xc5"),
            ParseContext("doc-1", "ambiguous.txt"),
        )

    assert error.value.code == "TEXT_ENCODING_UNCERTAIN"


def test_text_parser_never_silently_corrupts_accented_legacy_text():
    """Weak legacy detector evidence may reject, but may not return corrupt text."""
    source = "“Café déjà vu”"
    try:
        parsed = TextParser().parse(
            io.BytesIO(source.encode("cp1252")),
            ParseContext("doc-1", "accented.txt"),
        )
    except DocumentParseError as error:
        assert error.code == "TEXT_ENCODING_UNCERTAIN"
    else:
        assert [block.text for block in parsed.blocks] == [source]


def test_text_parser_rejects_blank_input_with_stable_error():
    """Returning an empty ParsedDocument must break this source-level invariant."""
    with pytest.raises(DocumentParseError) as error:
        TextParser().parse(io.BytesIO(b" \r\n\t\n"), ParseContext("doc-1", "empty.txt"))

    assert error.value.code == "NO_EXTRACTABLE_TEXT"


def test_markdown_parser_keeps_heading_path_and_code_block_atomic():
    """Splitting fences or losing heading context must break code block output."""
    source = b"# Install\n\nIntro\n\n```python\nprint('ok')\n```"

    parsed = MarkdownParser().parse(io.BytesIO(source), ParseContext("doc-1", "guide.md"))

    code = next(block for block in parsed.blocks if block.block_type == "code")
    assert code.text == "print('ok')\n"
    assert code.metadata["heading_path"] == ["Install"]
    assert code.metadata["language"] == "python"


def test_markdown_parser_emits_structure_and_document_metadata_without_html_execution():
    """Dropping Markdown structure or treating HTML as markup must break this output."""
    source = b"""---
title: Quick start
audience: operators
---

# Install

Read [the guide](https://example.test/guide).

- prepare
- run

| Name | Value |
| --- | --- |
| mode | safe |

<script>alert('never execute')</script>

## Verify

Ready.
"""

    parsed = MarkdownParser().parse(io.BytesIO(source), ParseContext("doc-1", "guide.md"))

    assert [(block.block_type, block.text) for block in parsed.blocks] == [
        ("heading", "Install"),
        ("paragraph", "Read the guide."),
        ("list_item", "prepare"),
        ("list_item", "run"),
        ("table_row", "Name | Value"),
        ("table_row", "mode | safe"),
        ("paragraph", "<script>alert('never execute')</script>"),
        ("heading", "Verify"),
        ("paragraph", "Ready."),
    ]
    assert parsed.blocks[-1].metadata["heading_path"] == ["Install", "Verify"]
    assert parsed.metadata == {
        "encoding": "utf-8-sig",
        "front_matter": {"title": "Quick start", "audience": "operators"},
        "links": ["https://example.test/guide"],
    }


def test_markdown_parser_rejects_document_without_extractable_blocks():
    """Returning a document with no blocks must break the parser output contract."""
    with pytest.raises(DocumentParseError) as error:
        MarkdownParser().parse(
            io.BytesIO(b"---\ntitle: Empty\n---\n"),
            ParseContext("doc-1", "empty.md"),
        )

    assert error.value.code == "NO_EXTRACTABLE_TEXT"


def test_markdown_parser_preserves_nested_list_and_scalar_front_matter():
    """Flattening front matter must lose the source document's metadata shape."""
    source = b"""---
title: Quick start
published: true
owners:
  - alice
  - bob
deployment:
  region: ap-southeast-1
  replicas: 2
---

# Install
"""

    parsed = MarkdownParser().parse(io.BytesIO(source), ParseContext("doc-1", "guide.md"))

    assert parsed.metadata["front_matter"] == {
        "title": "Quick start",
        "published": True,
        "owners": ["alice", "bob"],
        "deployment": {"region": "ap-southeast-1", "replicas": 2},
    }
    assert [block.text for block in parsed.blocks] == ["Install"]
    assert parsed.blocks[0].metadata["line_start"] == 12


def test_markdown_parser_preserves_malformed_front_matter_as_raw_metadata():
    """Discarding malformed YAML after removing it from the body must fail."""
    source = b"---\ntitle: [unterminated\n---\n\n# Install\n"

    parsed = MarkdownParser().parse(io.BytesIO(source), ParseContext("doc-1", "guide.md"))

    assert parsed.metadata["front_matter_raw"] == "title: [unterminated"
    assert [block.text for block in parsed.blocks] == ["Install"]
    assert parsed.blocks[0].metadata["line_start"] == 5


def test_markdown_parser_keeps_ambiguous_unclosed_delimiter_in_body():
    """Removing an unclosed front matter start must corrupt Markdown source content."""
    source = b"---\ntitle: still markdown\n\n# Install\n"

    parsed = MarkdownParser().parse(io.BytesIO(source), ParseContext("doc-1", "guide.md"))

    assert "front_matter" not in parsed.metadata
    assert "front_matter_raw" not in parsed.metadata
    assert [block.text for block in parsed.blocks] == ["title: still markdown", "Install"]


def test_markdown_parser_keeps_indented_delimiter_inside_yaml_block_scalar():
    """An indented scalar line is YAML content, never a front-matter boundary."""
    source = b"""---
summary: |
  first line
  ---
  last line
tags:
  - parser
---

# Install
"""

    parsed = MarkdownParser().parse(io.BytesIO(source), ParseContext("doc-1", "guide.md"))

    assert parsed.metadata["front_matter"] == {
        "summary": "first line\n---\nlast line\n",
        "tags": ["parser"],
    }
    assert [block.text for block in parsed.blocks] == ["Install"]
    assert parsed.blocks[0].metadata["line_start"] == 10
