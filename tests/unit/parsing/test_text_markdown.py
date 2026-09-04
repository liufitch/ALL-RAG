import io
import json

import pytest
from yaml.events import SequenceEndEvent, SequenceStartEvent

from rag_modules.parsing import markdown_parser
from rag_modules.parsing.base import ParseContext
from rag_modules.parsing.markdown_parser import MarkdownParser
from rag_modules.parsing.models import DocumentParseError
from rag_modules.parsing.text_parser import TextParser


def test_text_parser_rejects_short_gb18030_without_reliable_encoding_evidence():
    """A reversible short legacy decode is ambiguous and must not silently win."""
    with pytest.raises(DocumentParseError) as error:
        TextParser().parse(
            io.BytesIO("第一段\n\n第二段".encode("gb18030")),
            ParseContext("doc-1", "guide.txt"),
        )

    assert error.value.code == "TEXT_ENCODING_UNCERTAIN"


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


@pytest.mark.parametrize("encoding", ["utf-16-le", "utf-16-be"])
def test_text_parser_preserves_multiline_utf16_paragraphs_and_encoding(encoding):
    """Reversing UTF-16 control-byte lanes must reject ordinary multiline text."""
    parsed = TextParser().parse(
        io.BytesIO("first\n\nsecond".encode(encoding)),
        ParseContext("doc-1", "multiline.txt"),
    )

    assert [block.text for block in parsed.blocks] == ["first", "second"]
    assert [block.metadata for block in parsed.blocks] == [
        {"line_start": 1, "line_end": 1},
        {"line_start": 3, "line_end": 3},
    ]
    assert parsed.metadata["encoding"] == encoding.replace("-", "_")


@pytest.mark.parametrize("source", ["Plain Latin text", "Привет, мир"])
def test_text_parser_accepts_bomless_utf16_text_that_is_valid_utf8_bytes(source):
    """A low-quality UTF-8 decode must fall through to the UTF-16 evidence."""
    parsed = TextParser().parse(
        io.BytesIO(source.encode("utf-16-le")),
        ParseContext("doc-1", "utf16.txt"),
    )

    assert [block.text for block in parsed.blocks] == [source]
    assert parsed.metadata["encoding"] == "utf_16_le"


def test_text_parser_rejects_mixed_gb18030_without_reliable_encoding_evidence():
    """ASCII structure plus a round trip is not proof of one legacy encoding."""
    source = "Release 2: 中文, café — ready"
    with pytest.raises(DocumentParseError) as error:
        TextParser().parse(
            io.BytesIO(source.encode("gb18030")),
            ParseContext("doc-1", "mixed.txt"),
        )

    assert error.value.code == "TEXT_ENCODING_UNCERTAIN"


@pytest.mark.parametrize(
    ("source", "encoding"),
    [
        ("日本語の資料\nRelease 2", "shift_jis"),
        ("繁體中文報告\nRelease 2", "big5"),
        ("АБ\nRelease 2", "cp1251"),
    ],
)
def test_text_parser_never_reports_other_legacy_encodings_as_gb18030(source, encoding):
    """Selecting GB18030 for another reversible legacy stream corrupts source text."""
    with pytest.raises(DocumentParseError) as error:
        TextParser().parse(
            io.BytesIO(source.encode(encoding)),
            ParseContext("doc-1", "legacy.txt"),
        )

    assert error.value.code == "TEXT_ENCODING_UNCERTAIN"


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


def test_markdown_parser_normalizes_yaml_dates_as_json_strings():
    """Preflight must not reject SafeLoader dates that normalization can serialize."""
    source = b"---\npublished: 2026-09-04\n---\n# Release\n"

    parsed = MarkdownParser().parse(io.BytesIO(source), ParseContext("doc-1", "release.md"))

    assert parsed.metadata["front_matter"] == {"published": "2026-09-04"}
    assert [block.text for block in parsed.blocks] == ["Release"]


def test_markdown_parser_preserves_malformed_front_matter_as_raw_metadata():
    """Discarding malformed YAML after removing it from the body must fail."""
    source = b"---\ntitle: [unterminated\n---\n\n# Install\n"

    parsed = MarkdownParser().parse(io.BytesIO(source), ParseContext("doc-1", "guide.md"))

    assert parsed.metadata["front_matter_raw"] == "title: [unterminated"
    assert [block.text for block in parsed.blocks] == ["Install"]
    assert parsed.blocks[0].metadata["line_start"] == 5


def test_markdown_parser_preserves_duplicate_yaml_keys_as_raw_metadata():
    """Preflight must not bypass duplicate-key rejection during construction."""
    front_matter = "title: First\ntitle: Second"
    parsed = MarkdownParser().parse(
        io.BytesIO(f"---\n{front_matter}\n---\n# Release\n".encode()),
        ParseContext("doc-1", "duplicate-keys.md"),
    )

    assert "front_matter" not in parsed.metadata
    assert parsed.metadata["front_matter_raw"] == front_matter
    assert [block.text for block in parsed.blocks] == ["Release"]


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


@pytest.mark.parametrize(
    "front_matter",
    [
        "base: &base\n  role: reader\ncopy: *base",
        "loop: &loop\n  self: *loop",
    ],
)
def test_markdown_parser_never_exposes_yaml_alias_graphs(front_matter):
    """SafeLoader aliases can still create shared or recursive non-JSON metadata graphs."""
    parsed = MarkdownParser().parse(
        io.BytesIO(f"---\n{front_matter}\n---\n# Safe\n".encode()),
        ParseContext("doc-1", "aliases.md"),
    )

    assert "front_matter" not in parsed.metadata
    assert parsed.metadata["front_matter_raw"] == front_matter
    assert json.loads(json.dumps(parsed.metadata)) == parsed.metadata


@pytest.mark.parametrize(
    "front_matter",
    [
        "value: " + "x" * 20_000,
        "value: .nan",
        "value: .inf",
        "value:\n" + "  child:\n" * 25 + "    leaf: end",
        "items:\n" + "\n".join(f"  key_{index}: value" for index in range(10_001)),
    ],
)
def test_markdown_parser_bounds_front_matter_shape(front_matter):
    """Unbounded YAML scalars, depth, or node counts can exhaust preview serialization."""
    parsed = MarkdownParser().parse(
        io.BytesIO(f"---\n{front_matter}\n---\n# Safe\n".encode()),
        ParseContext("doc-1", "bounded.md"),
    )

    assert "front_matter" not in parsed.metadata
    assert len(parsed.metadata["front_matter_raw"]) <= 65_536
    assert json.loads(json.dumps(parsed.metadata)) == parsed.metadata


@pytest.mark.parametrize(
    "front_matter",
    [
        "- " * 1_500 + "leaf",
        "[" * 1_500 + "leaf" + "]" * 1_500,
    ],
    ids=["deep-block-sequences", "deep-flow-sequences"],
)
def test_markdown_parser_falls_back_safely_for_deep_yaml(front_matter):
    """Constructing deeply nested YAML must not leak a RecursionError."""
    parsed = MarkdownParser().parse(
        io.BytesIO(f"---\n{front_matter}\n---\n# Body\n\nStill parsed.\n".encode()),
        ParseContext("doc-1", "deep.md"),
    )

    assert [(block.block_type, block.text) for block in parsed.blocks] == [
        ("heading", "Body"),
        ("paragraph", "Still parsed."),
    ]
    assert "front_matter" not in parsed.metadata
    assert parsed.metadata["front_matter_raw"] == front_matter
    assert len(parsed.metadata["front_matter_raw"]) <= 65_536


def test_markdown_parser_rejects_yaml_event_budget_before_loading(monkeypatch):
    """A shallow event flood must stop before the value-constructing loader."""
    front_matter = "items:\n" + "  - x\n" * 9_999 + "  - x"

    def fail_if_loaded(*args, **kwargs):
        raise AssertionError("event preflight must run before yaml.load")

    monkeypatch.setattr(markdown_parser.yaml, "load", fail_if_loaded)

    parsed = MarkdownParser().parse(
        io.BytesIO(f"---\n{front_matter}\n---\n# Body\n".encode()),
        ParseContext("doc-1", "event-budget.md"),
    )

    assert [block.text for block in parsed.blocks] == ["Body"]
    assert "front_matter" not in parsed.metadata
    assert parsed.metadata["front_matter_raw"] == front_matter
    assert len(parsed.metadata["front_matter_raw"]) <= 65_536


def test_markdown_parser_falls_back_on_yaml_parse_recursion(monkeypatch):
    """A parser recursion failure must remain inside the metadata boundary."""
    def recurse(*args, **kwargs):
        raise RecursionError("synthetic yaml.parse recursion")

    monkeypatch.setattr(markdown_parser.yaml, "parse", recurse)

    parsed = MarkdownParser().parse(
        io.BytesIO(b"---\ntitle: Safe\n---\n# Body\n"),
        ParseContext("doc-1", "parse-recursion.md"),
    )

    assert [block.text for block in parsed.blocks] == ["Body"]
    assert parsed.metadata["front_matter_raw"] == "title: Safe"


def test_markdown_parser_falls_back_on_yaml_load_recursion(monkeypatch):
    """A loader recursion failure must remain inside the metadata boundary."""
    def recurse(*args, **kwargs):
        raise RecursionError("synthetic yaml.load recursion")

    monkeypatch.setattr(markdown_parser.yaml, "load", recurse)

    parsed = MarkdownParser().parse(
        io.BytesIO(b"---\ntitle: Safe\n---\n# Body\n"),
        ParseContext("doc-1", "load-recursion.md"),
    )

    assert [block.text for block in parsed.blocks] == ["Body"]
    assert parsed.metadata["front_matter_raw"] == "title: Safe"


def test_markdown_parser_falls_back_on_yaml_normalization_recursion(monkeypatch):
    """A normalization recursion failure must remain inside the metadata boundary."""
    def recurse(value):
        raise RecursionError("synthetic normalization recursion")

    monkeypatch.setattr(markdown_parser, "_normalize_front_matter", recurse)

    parsed = MarkdownParser().parse(
        io.BytesIO(b"---\ntitle: Safe\n---\n# Body\n"),
        ParseContext("doc-1", "normalization-recursion.md"),
    )

    assert [block.text for block in parsed.blocks] == ["Body"]
    assert parsed.metadata["front_matter_raw"] == "title: Safe"


@pytest.mark.parametrize(
    "front_matter",
    ["value: &scalar_anchor anchored", "value: *missing_alias"],
    ids=["scalar-anchor", "alias"],
)
def test_yaml_event_preflight_rejects_anchors_and_aliases(front_matter):
    """Removing all-event anchor checks must admit unsafe YAML graph syntax."""
    with pytest.raises(ValueError):
        markdown_parser._preflight_front_matter(front_matter)


@pytest.mark.parametrize(
    "events",
    [
        [SequenceEndEvent()],
        [SequenceStartEvent(anchor=None, tag=None, implicit=True, flow_style=False)],
    ],
    ids=["negative-depth", "unbalanced-depth"],
)
def test_yaml_event_preflight_rejects_invalid_collection_depth(monkeypatch, events):
    """Malformed event streams must not bypass the collection-depth invariant."""
    monkeypatch.setattr(
        markdown_parser.yaml, "parse", lambda *args, **kwargs: iter(events)
    )

    with pytest.raises(ValueError):
        markdown_parser._preflight_front_matter("ignored")


def test_markdown_parser_does_not_swallow_yaml_preflight_memory_error(monkeypatch):
    """Memory exhaustion is not a recoverable malformed-metadata condition."""
    def exhaust_memory(*args, **kwargs):
        raise MemoryError("synthetic exhaustion")

    monkeypatch.setattr(markdown_parser.yaml, "parse", exhaust_memory)

    with pytest.raises(MemoryError, match="synthetic exhaustion"):
        MarkdownParser().parse(
            io.BytesIO(b"---\ntitle: Safe\n---\n# Body\n"),
            ParseContext("doc-1", "memory-error.md"),
        )
