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
    """可往返还原的短旧编码文本仍可能存在歧义，不能无提示地选定它。"""
    with pytest.raises(DocumentParseError) as error:
        TextParser().parse(
            io.BytesIO("第一段\n\n第二段".encode("gb18030")),
            ParseContext("doc-1", "guide.txt"),
        )

    assert error.value.code == "TEXT_ENCODING_UNCERTAIN"


def test_text_parser_normalizes_bom_newlines_and_nuls_before_paragraph_split():
    """移除输入规范化后，生成的段落和来源范围应不再符合预期。"""
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
    """接受非文本字节会破坏解析器边界契约。"""
    with pytest.raises(DocumentParseError) as error:
        TextParser().parse(
            io.BytesIO(bytes(range(256)) * 4),
            ParseContext("doc-1", "blob.txt"),
        )

    assert error.value.code == "TEXT_ENCODING_UNCERTAIN"
    assert error.value.retryable is False


def test_text_parser_rejects_control_heavy_valid_utf8_before_nul_normalization():
    """UTF-8 解码后跳过质量检查，会放过类似二进制的文本。"""
    payload = (b"visible\x00 text\x01\x02\x03\x04" * 12)

    with pytest.raises(DocumentParseError) as error:
        TextParser().parse(io.BytesIO(payload), ParseContext("doc-1", "control.txt"))

    assert error.value.code == "TEXT_ENCODING_UNCERTAIN"


def test_text_parser_accepts_genuine_bomless_utf16_japanese_text():
    """拒绝无 BOM 的 UTF-16，或选中损坏的旧编码解码结果，都必须使测试失败。"""
    parsed = TextParser().parse(
        io.BytesIO("日本語のテキスト".encode("utf-16-le")),
        ParseContext("doc-1", "japanese.txt"),
    )

    assert [block.text for block in parsed.blocks] == ["日本語のテキスト"]
    assert parsed.metadata["encoding"] == "utf_16_le"


@pytest.mark.parametrize("encoding", ["utf-16-le", "utf-16-be"])
def test_text_parser_preserves_multiline_utf16_paragraphs_and_encoding(encoding):
    """颠倒 UTF-16 控制字节的位置，会错误拒绝普通多行文本。"""
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
    """质量较差的 UTF-8 解码结果，必须继续进入 UTF-16 证据检查。"""
    parsed = TextParser().parse(
        io.BytesIO(source.encode("utf-16-le")),
        ParseContext("doc-1", "utf16.txt"),
    )

    assert [block.text for block in parsed.blocks] == [source]
    assert parsed.metadata["encoding"] == "utf_16_le"


def test_text_parser_rejects_mixed_gb18030_without_reliable_encoding_evidence():
    """ASCII 结构和编码往返成功，并不能证明某一种旧编码就是正确编码。"""
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
    """对另一种同样可往返还原的旧编码流选择 GB18030，会损坏源文本。"""
    with pytest.raises(DocumentParseError) as error:
        TextParser().parse(
            io.BytesIO(source.encode(encoding)),
            ParseContext("doc-1", "legacy.txt"),
        )

    assert error.value.code == "TEXT_ENCODING_UNCERTAIN"


def test_text_parser_rejects_ambiguous_six_byte_non_utf8_payload():
    """任意选中一种同样有效的旧编码解码结果，会破坏对编码不确定性的处理边界。"""
    with pytest.raises(DocumentParseError) as error:
        TextParser().parse(
            io.BytesIO(b"\xc0\xc1\xc2\xc3\xc4\xc5"),
            ParseContext("doc-1", "ambiguous.txt"),
        )

    assert error.value.code == "TEXT_ENCODING_UNCERTAIN"


def test_text_parser_never_silently_corrupts_accented_legacy_text():
    """旧编码检测证据不足时可以拒绝输入，但不得返回损坏的文本。"""
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
    """返回空的 ParsedDocument 会破坏源文档级的不变量。"""
    with pytest.raises(DocumentParseError) as error:
        TextParser().parse(io.BytesIO(b" \r\n\t\n"), ParseContext("doc-1", "empty.txt"))

    assert error.value.code == "NO_EXTRACTABLE_TEXT"


def test_markdown_parser_keeps_heading_path_and_code_block_atomic():
    """拆开围栏代码块或丢失标题上下文，会破坏代码块输出。"""
    source = b"# Install\n\nIntro\n\n```python\nprint('ok')\n```"

    parsed = MarkdownParser().parse(io.BytesIO(source), ParseContext("doc-1", "guide.md"))

    code = next(block for block in parsed.blocks if block.block_type == "code")
    assert code.text == "print('ok')\n"
    assert code.metadata["heading_path"] == ["Install"]
    assert code.metadata["language"] == "python"


def test_markdown_parser_emits_structure_and_document_metadata_without_html_execution():
    """丢弃 Markdown 结构或将 HTML 当作标记解析，会破坏预期输出。"""
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
    """返回没有任何内容块的文档，会破坏解析器输出契约。"""
    with pytest.raises(DocumentParseError) as error:
        MarkdownParser().parse(
            io.BytesIO(b"---\ntitle: Empty\n---\n"),
            ParseContext("doc-1", "empty.md"),
        )

    assert error.value.code == "NO_EXTRACTABLE_TEXT"


def test_markdown_parser_preserves_nested_list_and_scalar_front_matter():
    """将文首元数据扁平化，会丢失源文档的元数据结构。"""
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
    """预检不得拒绝可由规范化流程序列化的 SafeLoader 日期值。"""
    source = b"---\npublished: 2026-09-04\n---\n# Release\n"

    parsed = MarkdownParser().parse(io.BytesIO(source), ParseContext("doc-1", "release.md"))

    assert parsed.metadata["front_matter"] == {"published": "2026-09-04"}
    assert [block.text for block in parsed.blocks] == ["Release"]


def test_markdown_parser_preserves_malformed_front_matter_as_raw_metadata():
    """将格式错误的 YAML 从正文移除后再丢弃它，必须使测试失败。"""
    source = b"---\ntitle: [unterminated\n---\n\n# Install\n"

    parsed = MarkdownParser().parse(io.BytesIO(source), ParseContext("doc-1", "guide.md"))

    assert parsed.metadata["front_matter_raw"] == "title: [unterminated"
    assert [block.text for block in parsed.blocks] == ["Install"]
    assert parsed.blocks[0].metadata["line_start"] == 5


def test_markdown_parser_preserves_duplicate_yaml_keys_as_raw_metadata():
    """预检不得绕过构造阶段的重复键检查。"""
    front_matter = "title: First\ntitle: Second"
    parsed = MarkdownParser().parse(
        io.BytesIO(f"---\n{front_matter}\n---\n# Release\n".encode()),
        ParseContext("doc-1", "duplicate-keys.md"),
    )

    assert "front_matter" not in parsed.metadata
    assert parsed.metadata["front_matter_raw"] == front_matter
    assert [block.text for block in parsed.blocks] == ["Release"]


def test_markdown_parser_keeps_ambiguous_unclosed_delimiter_in_body():
    """移除未闭合的文首元数据起始标记，会损坏 Markdown 源内容。"""
    source = b"---\ntitle: still markdown\n\n# Install\n"

    parsed = MarkdownParser().parse(io.BytesIO(source), ParseContext("doc-1", "guide.md"))

    assert "front_matter" not in parsed.metadata
    assert "front_matter_raw" not in parsed.metadata
    assert [block.text for block in parsed.blocks] == ["title: still markdown", "Install"]


def test_markdown_parser_keeps_indented_delimiter_inside_yaml_block_scalar():
    """缩进的标量行属于 YAML 内容，绝不能视为文首元数据的边界。"""
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
    """SafeLoader 别名仍可能创建含共享引用或循环引用、不兼容 JSON 的元数据图。"""
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
    """不限制 YAML 标量大小、嵌套深度或节点数量，可能耗尽预览序列化资源。"""
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
    """构造深层嵌套 YAML 时，不得直接暴露 RecursionError。"""
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
    """大量浅层事件必须在进入值构造加载器前被拦截。"""
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
    """解析器递归异常必须在元数据处理边界内处理。"""
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
    """加载器递归异常必须在元数据处理边界内处理。"""
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
    """规范化递归异常必须在元数据处理边界内处理。"""
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
    """移除对所有事件的锚点检查，会放过不安全的 YAML 图语法。"""
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
    """格式错误的事件流不得绕过集合嵌套深度的不变量。"""
    monkeypatch.setattr(
        markdown_parser.yaml, "parse", lambda *args, **kwargs: iter(events)
    )

    with pytest.raises(ValueError):
        markdown_parser._preflight_front_matter("ignored")


def test_markdown_parser_does_not_swallow_yaml_preflight_memory_error(monkeypatch):
    """内存耗尽不属于可恢复的元数据格式错误。"""
    def exhaust_memory(*args, **kwargs):
        raise MemoryError("synthetic exhaustion")

    monkeypatch.setattr(markdown_parser.yaml, "parse", exhaust_memory)

    with pytest.raises(MemoryError, match="synthetic exhaustion"):
        MarkdownParser().parse(
            io.BytesIO(b"---\ntitle: Safe\n---\n# Body\n"),
            ParseContext("doc-1", "memory-error.md"),
        )
