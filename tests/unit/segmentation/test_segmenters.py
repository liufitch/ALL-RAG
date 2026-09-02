"""Contracts for parser-output segmentation; these tests exercise no external services."""

import pytest

from rag_modules.parsing.models import ParsedBlock, ParsedDocument
from rag_modules.segmentation import (
    GeneralSegmentationConfig,
    ParentChildSegmentationConfig,
    SegmentationConfigError,
    Segmenter,
)


def parsed_document(*blocks: ParsedBlock, source_type: str = "text") -> ParsedDocument:
    return ParsedDocument("doc-1", "source.txt", source_type, blocks, {})


def test_general_segmentation_honors_unicode_character_limit_and_overlap():
    """Ignoring Unicode length or overlap would make preview chunks unsafe to index."""
    parsed = parsed_document(ParsedBlock("paragraph", "第一句。第二句。第三句。", {"line_start": 1, "line_end": 1}))

    result = Segmenter().segment(parsed, GeneralSegmentationConfig(max_chunk_length=8, overlap=2))

    assert [item.content for item in result.segments] == ["第一句。第二句。", "句。第三句。"]
    assert all(0 < len(item.content) <= 8 for item in result.segments)
    assert result.segments[0].content[-2:] == result.segments[1].content[:2]
    assert [item.local_id for item in result.segments] == ["s-000001", "s-000002"]


def test_general_prefers_configured_then_paragraph_newline_sentence_and_word_boundaries():
    """Changing the priority must produce less useful boundaries for the same source."""
    parsed = parsed_document(
        ParsedBlock("paragraph", "aa|bb\n\ncc\ndd。ee! ff gg", {"line_start": 1, "line_end": 4})
    )

    configured = Segmenter().segment(parsed, GeneralSegmentationConfig(max_chunk_length=5, overlap=0, separator="|"))
    paragraphs = Segmenter().segment(parsed, GeneralSegmentationConfig(max_chunk_length=6, overlap=0))

    assert [item.content for item in configured.segments][:2] == ["aa|", "bb\n\n"]
    assert [item.content for item in paragraphs.segments][:2] == ["aa|bb\n", "\ncc\n"]
    assert "".join(item.content for item in paragraphs.segments) == "aa|bb\n\ncc\ndd。ee! ff gg"


@pytest.mark.parametrize("text", ["abcdefghijk", "中文甲乙丙丁戊己庚辛壬癸"])
def test_general_hard_splits_content_without_any_boundary(text):
    """Removing the final hard split would emit chunks beyond the configured limit."""
    result = Segmenter().segment(
        parsed_document(ParsedBlock("paragraph", text, {"line_start": 1, "line_end": 1})),
        GeneralSegmentationConfig(max_chunk_length=4, overlap=0),
    )

    assert [item.content for item in result.segments] == [text[0:4], text[4:8], text[8:]]


def test_general_keeps_code_and_table_rows_atomic_until_the_hard_limit():
    """Joining atomic parser blocks into prose would split short code and table facts."""
    parsed = parsed_document(
        ParsedBlock("code", "x = 1", {"heading_path": ["Install"], "line_start": 3, "line_end": 3}),
        ParsedBlock("table_row", "名称：甲；值：乙", {"sheet": "Data", "row": 2, "headers": ["名称", "值"]}),
    )

    result = Segmenter().segment(parsed, GeneralSegmentationConfig(max_chunk_length=20, overlap=0))

    assert [item.content for item in result.segments] == ["x = 1", "名称：甲；值：乙"]
    assert result.segments[0].source_metadata["heading_path"] == ["Install"]
    assert result.segments[1].source_metadata["headers"] == ["名称", "值"]
    oversized = Segmenter().segment(parsed_document(ParsedBlock("code", "abcdef", {})), GeneralSegmentationConfig(max_chunk_length=4, overlap=0))
    assert [item.content for item in oversized.segments] == ["abcd", "ef"]


def test_general_merges_source_ranges_without_losing_heading_metadata():
    """Dropping a block's source range or heading makes a preview result untraceable."""
    parsed = parsed_document(
        ParsedBlock("paragraph", "Alpha", {"line_start": 3, "line_end": 3, "heading_path": ["A"]}),
        ParsedBlock("paragraph", "Beta", {"line_start": 5, "line_end": 5, "heading_path": ["B"]}),
    )

    result = Segmenter().segment(parsed, GeneralSegmentationConfig(max_chunk_length=20, overlap=0))

    assert result.segments[0].content == "Alpha\n\nBeta"
    assert result.segments[0].source_metadata == {
        "line_start": 3,
        "line_end": 5,
        "heading_path": ["A"],
        "heading_paths": [["A"], ["B"]],
    }


@pytest.mark.parametrize(
    "config",
    [
        GeneralSegmentationConfig(max_chunk_length=0, overlap=0),
        GeneralSegmentationConfig(max_chunk_length=4, overlap=4),
        ParentChildSegmentationConfig(parent_mode="paragraph", parent_max_length=0, child_max_length=4, child_overlap=0),
        ParentChildSegmentationConfig(parent_mode="paragraph", parent_max_length=4, child_max_length=4, child_overlap=4),
    ],
)
def test_segmenter_rejects_invalid_explicit_configurations(config):
    """Permitting invalid limits could make the overlap loop never advance."""
    with pytest.raises(SegmentationConfigError) as error:
        Segmenter().segment(parsed_document(ParsedBlock("paragraph", "text", {})), config)

    assert error.value.code == "INVALID_SEGMENTATION_CONFIG"


def test_parent_child_emits_parent_before_children_and_links_them_deterministically():
    """Changing output order or IDs would orphan children in the indexing worker."""
    parsed = parsed_document(ParsedBlock("paragraph", "第一段很长。第二句继续。", {"line_start": 1, "line_end": 1}))
    config = ParentChildSegmentationConfig("paragraph", 20, 8, 1)

    first = Segmenter().segment(parsed, config)
    second = Segmenter().segment(parsed, config)

    assert [(item.local_id, item.parent_local_id, item.position, item.index_type) for item in first.segments] == [
        ("p-000001", None, 0, "parent"),
        ("c-000001", "p-000001", 1, "child"),
        ("c-000002", "p-000001", 2, "child"),
    ]
    assert [item.content for item in first.segments[1:]] == ["第一段很长。", "。第二句继续。"]
    assert first == second


def test_parent_modes_never_emit_parent_above_limit_and_warn_on_full_document_fallback():
    """A full-document parent larger than its limit must degrade predictably, not overflow."""
    parsed = parsed_document(
        ParsedBlock("paragraph", "one two", {"line_start": 1, "line_end": 1}),
        ParsedBlock("paragraph", "three four", {"line_start": 3, "line_end": 3}),
    )

    result = Segmenter().segment(parsed, ParentChildSegmentationConfig("full_document", 8, 4, 0))

    parents = [item for item in result.segments if item.index_type == "parent"]
    assert all(len(item.content) <= 8 for item in parents)
    assert "".join(item.content for item in parents) == "one two\n\nthree four"
    assert [(warning.code, warning.metadata) for warning in result.warnings] == [
        ("PARENT_FULL_DOCUMENT_FALLBACK", {"parent_max_length": 8})
    ]


def test_full_document_fallback_keeps_short_code_atomic():
    """Splitting a short code block during fallback destroys a parser-level atomic unit."""
    parsed = parsed_document(
        ParsedBlock("paragraph", "intro", {"line_start": 1, "line_end": 1}),
        ParsedBlock("code", "x = 1", {"line_start": 3, "line_end": 3, "language": "python"}),
        ParsedBlock("paragraph", "outro", {"line_start": 5, "line_end": 5}),
    )

    result = Segmenter().segment(parsed, ParentChildSegmentationConfig("full_document", 8, 8, 0))

    parents = [item for item in result.segments if item.index_type == "parent"]
    code_parent = next(item for item in parents if item.content == "x = 1")
    assert code_parent.source_metadata["language"] == "python"


def test_spreadsheet_groups_consecutive_rows_per_sheet_and_preserves_headers_in_children():
    """Cross-sheet grouping or omitted headers makes spreadsheet children ambiguous."""
    parsed = parsed_document(
        ParsedBlock("table_row", "名称：甲", {"sheet": "One", "row": 2, "headers": ["名称"]}),
        ParsedBlock("table_row", "名称：乙", {"sheet": "One", "row": 3, "headers": ["名称"]}),
        ParsedBlock("table_row", "名称：丙", {"sheet": "Two", "row": 2, "headers": ["名称"]}),
        source_type="xlsx",
    )

    result = Segmenter().segment(parsed, ParentChildSegmentationConfig("paragraph", 30, 10, 0))

    parents = [item for item in result.segments if item.index_type == "parent"]
    children = [item for item in result.segments if item.index_type == "child"]
    assert [(item.content, item.source_metadata) for item in parents] == [
        ("名称：甲\n名称：乙", {"sheet": "One", "row_start": 2, "row_end": 3, "headers": ["名称"]}),
        ("名称：丙", {"sheet": "Two", "row": 2, "headers": ["名称"]}),
    ]
    assert [(item.parent_local_id, item.content, item.source_metadata["headers"]) for item in children] == [
        ("p-000001", "名称：甲", ["名称"]),
        ("p-000001", "名称：乙", ["名称"]),
        ("p-000002", "名称：丙", ["名称"]),
    ]


def test_spreadsheet_starts_a_new_parent_when_source_row_numbers_are_not_consecutive():
    """Merging rows across a source-row gap makes parent provenance falsely contiguous."""
    parsed = parsed_document(
        ParsedBlock("table_row", "名称：甲", {"sheet": "One", "row": 2, "headers": ["名称"]}),
        ParsedBlock("table_row", "名称：乙", {"sheet": "One", "row": 4, "headers": ["名称"]}),
        source_type="xlsx",
    )

    result = Segmenter().segment(parsed, ParentChildSegmentationConfig("paragraph", 30, 10, 0))

    parents = [item for item in result.segments if item.index_type == "parent"]
    assert [(item.content, item.source_metadata["row"]) for item in parents] == [
        ("名称：甲", 2),
        ("名称：乙", 4),
    ]


def test_empty_parsed_document_returns_an_empty_stable_result():
    """Fabricating a segment for empty parser output would create an unsearchable record."""
    result = Segmenter().segment(parsed_document(), GeneralSegmentationConfig(max_chunk_length=8, overlap=0))

    assert result.segments == ()
    assert result.warnings == ()
