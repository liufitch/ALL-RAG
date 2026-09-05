"""验证解析结果的分段契约；这些测试不访问外部服务。"""

import signal
from contextlib import contextmanager

import pytest

from rag_modules.parsing.models import ParsedBlock, ParsedDocument
from rag_modules.segmentation import segmenter as segmenter_module
from rag_modules.segmentation import (
    GeneralSegmentationConfig,
    ParentChildSegmentationConfig,
    SegmentationConfigError,
    Segmenter,
)


def parsed_document(*blocks: ParsedBlock, source_type: str = "text") -> ParsedDocument:
    return ParsedDocument("doc-1", "source.txt", source_type, blocks, {})


class _SegmentationDeadlineExpired(Exception):
    pass


@contextmanager
def segmentation_deadline(seconds: float = 0.1):
    """对已知无法推进的回归问题设置执行上限，且不遗留进程。"""
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, 0)

    def expire(_signum, _frame):
        raise _SegmentationDeadlineExpired("segmenter did not make progress before deadline")

    signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0]:
            signal.setitimer(signal.ITIMER_REAL, *previous_timer)


def rebuild_with_overlap(segments, overlap: int) -> str:
    return segments[0].content + "".join(item.content[overlap:] for item in segments[1:])


def test_general_segmentation_honors_unicode_character_limit_and_overlap():
    """忽略 Unicode 长度或重叠量会使预览分段无法安全建立索引。"""
    parsed = parsed_document(ParsedBlock("paragraph", "第一句。第二句。第三句。", {"line_start": 1, "line_end": 1}))

    result = Segmenter().segment(parsed, GeneralSegmentationConfig(max_chunk_length=8, overlap=2))

    assert [item.content for item in result.segments] == ["第一句。第二句。", "句。第三句。"]
    assert all(0 < len(item.content) <= 8 for item in result.segments)
    assert result.segments[0].content[-2:] == result.segments[1].content[:2]
    assert [item.local_id for item in result.segments] == ["s-000001", "s-000002"]


def test_general_makes_progress_when_a_preferred_boundary_is_not_past_overlap():
    """重复使用推进距离不超过重叠量的边界时，不得在同一输入范围内循环。"""
    source = "a。abcdefgh"
    with segmentation_deadline():
        result = Segmenter().segment(
            parsed_document(ParsedBlock("paragraph", source, {})),
            GeneralSegmentationConfig(max_chunk_length=8, overlap=2),
        )

    assert all(0 < len(item.content) <= 8 for item in result.segments)
    assert all(
        left.content[-2:] == right.content[:2]
        for left, right in zip(result.segments, result.segments[1:])
    )
    assert rebuild_with_overlap(result.segments, 2) == source


def test_general_rejects_projected_extreme_overlap_before_boundary_scanning(monkeypatch):
    """如果仅检查已生成的分段，拒绝输入前可能耗费数百万次迭代。"""
    boundary_calls = 0

    def fail_if_called(*_args, **_kwargs):
        nonlocal boundary_calls
        boundary_calls += 1
        raise AssertionError("projected work must be rejected before boundary scanning")

    monkeypatch.setattr(segmenter_module, "_boundary_end", fail_if_called)

    with pytest.raises(SegmentationConfigError) as error:
        Segmenter(max_segments=10_000).segment(
            parsed_document(ParsedBlock("paragraph", "x" * 5_000_000, {})),
            GeneralSegmentationConfig(max_chunk_length=1_000_000, overlap=999_999),
        )

    assert error.value.code == "SEGMENTATION_LIMIT_EXCEEDED"
    assert boundary_calls == 0


def test_parent_child_rejects_projected_extreme_overlap_before_boundary_scanning(monkeypatch):
    """子分段拆分器必须共用请求预算，并在首次扫描前拒绝超限输入。"""
    boundary_calls = 0

    def fail_if_called(*_args, **_kwargs):
        nonlocal boundary_calls
        boundary_calls += 1
        raise AssertionError("projected child work must be rejected before boundary scanning")

    monkeypatch.setattr(segmenter_module, "_boundary_end", fail_if_called)
    source = "x" * 5_000_000

    with pytest.raises(SegmentationConfigError) as error:
        Segmenter(max_segments=10_000).segment(
            parsed_document(ParsedBlock("paragraph", source, {})),
            ParentChildSegmentationConfig(
                "paragraph",
                parent_max_length=len(source),
                child_max_length=1_000_000,
                child_overlap=999_999,
            ),
        )

    assert error.value.code == "SEGMENTATION_LIMIT_EXCEEDED"
    assert boundary_calls == 0


def test_boundary_progress_falls_back_early_but_keeps_later_preferred_delimiter():
    """合法但过小的首选推进量，不得突破已验证的最小推进量保障。"""
    source = "aaaaa|bbbbcccc|dddd"

    result = Segmenter().segment(
        parsed_document(ParsedBlock("paragraph", source, {})),
        GeneralSegmentationConfig(max_chunk_length=10, overlap=4, separator="|"),
    )

    assert [item.content for item in result.segments] == [
        "aaaaa|bbbb",
        "bbbbcccc|",
        "ccc|dddd",
    ]
    assert rebuild_with_overlap(result.segments, 4) == source


def test_parent_child_makes_progress_when_a_preferred_child_boundary_is_not_past_overlap():
    """父子分段中的子级处理路径共用拆分器，必须具有相同的推进保障。"""
    source = "a。abcdefgh"
    with segmentation_deadline():
        result = Segmenter().segment(
            parsed_document(ParsedBlock("paragraph", source, {})),
            ParentChildSegmentationConfig("paragraph", 20, 8, 2),
        )

    children = [item for item in result.segments if item.index_type == "child"]
    assert all(0 < len(item.content) <= 8 for item in children)
    assert all(
        left.content[-2:] == right.content[:2] for left, right in zip(children, children[1:])
    )
    assert rebuild_with_overlap(children, 2) == source


def test_general_prefers_configured_then_paragraph_newline_sentence_and_word_boundaries():
    """修改边界优先级后，同一源文本的分段边界质量应下降。"""
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
    """移除最终的强制拆分步骤，会生成超出配置长度上限的分段。"""
    result = Segmenter().segment(
        parsed_document(ParsedBlock("paragraph", text, {"line_start": 1, "line_end": 1})),
        GeneralSegmentationConfig(max_chunk_length=4, overlap=0),
    )

    assert [item.content for item in result.segments] == [text[0:4], text[4:8], text[8:]]


def test_general_keeps_code_and_table_rows_atomic_until_the_hard_limit():
    """将原子的解析块并入普通文本，会拆散较短的代码块和表格事实。"""
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
    """丢弃块的来源范围或标题，会使预览结果无法追溯。"""
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
    """允许无效限制值，可能导致处理重叠区间的循环无法推进。"""
    with pytest.raises(SegmentationConfigError) as error:
        Segmenter().segment(parsed_document(ParsedBlock("paragraph", "text", {})), config)

    assert error.value.code == "INVALID_SEGMENTATION_CONFIG"


def test_parent_child_emits_parent_before_children_and_links_them_deterministically():
    """改变输出顺序或标识，会使索引工作任务中的子分段失去父级关联。"""
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
    """全文父分段超出长度上限时，必须按确定的方式降级处理，不能溢出。"""
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
    """在兜底处理时拆分较短的代码块，会破坏解析器级别的原子单元。"""
    parsed = parsed_document(
        ParsedBlock("paragraph", "intro", {"line_start": 1, "line_end": 1}),
        ParsedBlock("code", "x = 1", {"line_start": 3, "line_end": 3, "language": "python"}),
        ParsedBlock("paragraph", "outro", {"line_start": 5, "line_end": 5}),
    )

    result = Segmenter().segment(parsed, ParentChildSegmentationConfig("full_document", 8, 8, 0))

    parents = [item for item in result.segments if item.index_type == "parent"]
    code_parent = next(item for item in parents if item.content == "x = 1")
    assert code_parent.source_metadata["language"] == "python"


@pytest.mark.parametrize(
    "atomic_type, atomic_text, atomic_metadata",
    [
        ("code", "x = 1", {"language": "python", "line_start": 3, "line_end": 3}),
        ("table_row", "名称：甲", {"sheet": "Data", "row": 2, "headers": ["名称"]}),
    ],
)
def test_full_document_fallback_preserves_delimiters_around_short_atomic_blocks(
    atomic_type, atomic_text, atomic_metadata
):
    """兜底处理应准确保留原始分隔符，同时不修改长度合规的原子块。"""
    blocks = (
        ParsedBlock("paragraph", "aa", {"line_start": 1, "line_end": 1}),
        ParsedBlock(atomic_type, atomic_text, atomic_metadata),
        ParsedBlock("paragraph", "bb", {"line_start": 5, "line_end": 5}),
    )
    parsed = parsed_document(*blocks)

    result = Segmenter().segment(parsed, ParentChildSegmentationConfig("full_document", 8, 8, 0))

    parents = [item for item in result.segments if item.index_type == "parent"]
    assert "".join(item.content for item in parents) == f"aa\n\n{atomic_text}\n\nbb"
    assert all(item.content.strip() and len(item.content) <= 8 for item in parents)
    assert all(
        item.content.strip() and len(item.content) <= 8
        for item in result.segments
        if item.index_type == "child"
    )
    assert all(item.source_metadata for item in result.segments)
    assert next(item for item in parents if item.content == atomic_text).source_metadata == atomic_metadata
    assert [item.position for item in result.segments] == list(range(len(result.segments)))
    parent_positions = {
        item.local_id: item.position for item in result.segments if item.index_type == "parent"
    }
    assert all(
        item.parent_local_id in parent_positions
        and parent_positions[item.parent_local_id] < item.position
        for item in result.segments
        if item.index_type == "child"
    )
    assert [warning.code for warning in result.warnings] == ["PARENT_FULL_DOCUMENT_FALLBACK"]


@pytest.mark.parametrize("maximum", [1, 2])
def test_full_document_fallback_delimiter_omission_is_bounded_and_aggregated(maximum):
    """无法保留的分隔符必须只省略一次，不得生成空白记录或陷入循环。"""
    parsed = parsed_document(
        ParsedBlock("paragraph", "A", {"line_start": 1}),
        ParsedBlock("code", "B", {"line_start": 3, "language": "text"}),
        ParsedBlock("paragraph", "C", {"line_start": 5}),
    )

    with segmentation_deadline():
        result = Segmenter().segment(
            parsed,
            ParentChildSegmentationConfig("full_document", maximum, maximum, 0),
        )

    parents = [item for item in result.segments if item.index_type == "parent"]
    assert "".join(item.content for item in parents) == "ABC"
    assert all(item.content.strip() and len(item.content) <= maximum for item in result.segments)
    assert next(item for item in parents if item.content == "B").source_metadata == {
        "line_start": 3,
        "line_end": 3,
        "language": "text",
    }
    assert [item.position for item in result.segments] == list(range(len(result.segments)))
    parent_positions = {
        item.local_id: item.position for item in result.segments if item.index_type == "parent"
    }
    assert all(
        item.parent_local_id in parent_positions
        and parent_positions[item.parent_local_id] < item.position
        for item in result.segments
        if item.index_type == "child"
    )
    assert [
        (warning.code, warning.message, warning.metadata) for warning in result.warnings
    ] == [
        (
            "PARENT_FULL_DOCUMENT_FALLBACK",
            "The full-document parent exceeded its maximum and was split.",
            {"parent_max_length": maximum},
        ),
        (
            "SEGMENT_DELIMITER_OMITTED",
            "A source delimiter could not be retained within the configured parent length.",
            {"delimiter": "\\n\\n", "count": 2},
        ),
    ]


def test_full_document_fallback_uses_following_prose_when_preceding_parent_is_full():
    """后续源文本能够容纳分隔符时，无需拆分已占满的前一段源文本。"""
    parsed = parsed_document(
        ParsedBlock("paragraph", "ABCDEFGH", {"line_start": 1}),
        ParsedBlock("paragraph", "bb", {"line_start": 3}),
    )

    result = Segmenter().segment(
        parsed,
        ParentChildSegmentationConfig("full_document", 8, 8, 0),
    )

    parents = [item.content for item in result.segments if item.index_type == "parent"]
    assert parents == ["ABCDEFGH", "\n\nbb"]
    assert [warning.code for warning in result.warnings] == ["PARENT_FULL_DOCUMENT_FALLBACK"]


def test_full_document_fallback_splits_delimiter_across_non_atomic_sides_when_needed():
    """长度为 2 时可以保留拆开的分隔符，不得错误地将其省略。"""
    parsed = parsed_document(
        ParsedBlock("paragraph", "A", {"line_start": 1}),
        ParsedBlock("paragraph", "B", {"line_start": 3}),
    )

    result = Segmenter().segment(
        parsed,
        ParentChildSegmentationConfig("full_document", 2, 2, 0),
    )

    parents = [item.content for item in result.segments if item.index_type == "parent"]
    assert parents == ["A\n", "\nB"]
    assert all(content.strip() for content in parents)
    assert [warning.code for warning in result.warnings] == ["PARENT_FULL_DOCUMENT_FALLBACK"]


def test_full_document_fallback_resolves_three_block_delimiter_contention():
    """独立的贪心边界选择，不得省略本可通过联合安排保留的分隔符。"""
    parsed = parsed_document(
        ParsedBlock("paragraph", "AA", {"line_start": 1}),
        ParsedBlock("paragraph", "B", {"line_start": 3}),
        ParsedBlock("paragraph", " C", {"line_start": 5}),
    )

    result = Segmenter().segment(
        parsed,
        ParentChildSegmentationConfig("full_document", 3, 3, 0),
    )

    parents = [item for item in result.segments if item.index_type == "parent"]
    assert [item.content for item in parents] == ["AA\n", "\nB\n", "\n C"]
    assert "".join(item.content for item in parents) == "AA\n\nB\n\n C"
    assert all(item.content.strip() and len(item.content) <= 3 for item in parents)
    assert [item.source_metadata["line_start"] for item in parents] == [1, 3, 5]
    assert [item.position for item in result.segments] == list(range(len(result.segments)))
    parent_positions = {
        item.local_id: item.position for item in result.segments if item.index_type == "parent"
    }
    assert all(
        item.parent_local_id in parent_positions
        and parent_positions[item.parent_local_id] < item.position
        for item in result.segments
        if item.index_type == "child"
    )
    assert [warning.code for warning in result.warnings] == ["PARENT_FULL_DOCUMENT_FALLBACK"]


@pytest.mark.parametrize(
    ("maximum", "texts"),
    [
        (2, ("A", "B")),
        (3, ("AA", "B", " C")),
        (4, ("AAA", "B", " C")),
    ],
)
def test_full_document_fallback_contention_matrix_retains_all_delimiters(maximum, texts):
    """连续的短文本片段不得导致分隔符重复或丢失。"""
    parsed = parsed_document(
        *(
            ParsedBlock("paragraph", text, {"line_start": 2 * index + 1})
            for index, text in enumerate(texts)
        )
    )

    result = Segmenter().segment(
        parsed,
        ParentChildSegmentationConfig("full_document", maximum, maximum, 0),
    )

    parents = [item.content for item in result.segments if item.index_type == "parent"]
    assert "".join(parents) == "\n\n".join(texts)
    assert all(content.strip() and len(content) <= maximum for content in parents)
    assert [warning.code for warning in result.warnings] == ["PARENT_FULL_DOCUMENT_FALLBACK"]


def test_spreadsheet_groups_consecutive_rows_per_sheet_and_preserves_headers_in_children():
    """跨工作表分组或省略表头，会使电子表格的子分段含义不清。"""
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
    """跨源行间隔合并数据行，会让父分段的来源范围错误地显示为连续。"""
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
    """为空解析结果虚构分段，会产生无法检索的记录。"""
    result = Segmenter().segment(parsed_document(), GeneralSegmentationConfig(max_chunk_length=8, overlap=0))

    assert result.segments == ()
    assert result.warnings == ()


@pytest.mark.parametrize(
    "config",
    [
        GeneralSegmentationConfig(max_chunk_length=1, overlap=0),
        ParentChildSegmentationConfig("paragraph", 1, 1, 0),
        ParentChildSegmentationConfig("full_document", 1, 1, 0),
    ],
)
def test_every_public_segment_has_nonblank_content(config):
    """纯空白块和合成分隔符不得成为可检索记录。"""
    parsed = parsed_document(
        ParsedBlock("paragraph", "   \n\t", {"line_start": 1}),
        ParsedBlock("paragraph", "A", {"line_start": 2}),
        ParsedBlock("code", "B", {"line_start": 4}),
        ParsedBlock("paragraph", "C", {"line_start": 6}),
    )

    result = Segmenter().segment(parsed, config)

    assert result.segments
    assert all(segment.content.strip() for segment in result.segments)
    assert "".join(segment.content.strip() for segment in result.segments if segment.index_type != "child")


def test_segmenter_stops_before_materializing_unbounded_tiny_chunks():
    """按单个字符拆分时，必须在有限工作量内触发稳定的分段数量上限。"""
    parsed = parsed_document(ParsedBlock("paragraph", "x" * 100, {}))

    with segmentation_deadline(), pytest.raises(SegmentationConfigError) as error:
        Segmenter(max_segments=8).segment(
            parsed,
            GeneralSegmentationConfig(max_chunk_length=1, overlap=0),
        )

    assert error.value.code == "SEGMENTATION_LIMIT_EXCEEDED"


@pytest.mark.parametrize(
    "parsed",
    [
        parsed_document(*(ParsedBlock("paragraph", "x", {}) for _ in range(9))),
        parsed_document(ParsedBlock("paragraph", "x" * 9, {})),
    ],
)
def test_segmenter_has_an_independent_source_work_budget(parsed):
    """不能只限制输出，还必须限制源块数量和字符扫描量。"""
    segmenter = Segmenter(max_source_blocks=8, max_source_characters=8)

    with pytest.raises(SegmentationConfigError) as error:
        segmenter.segment(
            parsed,
            GeneralSegmentationConfig(max_chunk_length=8, overlap=0),
        )

    assert error.value.code == "SEGMENTATION_LIMIT_EXCEEDED"


def test_boundary_scan_budget_is_cumulative_across_general_source_blocks():
    """若对每个原子源块重置扫描预算，总扫描量就可能失去上限。"""
    parsed = parsed_document(
        ParsedBlock("code", "abc|def", {"line_start": 1}),
        ParsedBlock("code", "abc|def", {"line_start": 3}),
    )
    config = GeneralSegmentationConfig(max_chunk_length=4, overlap=0, separator="|")

    with pytest.raises(SegmentationConfigError) as error:
        Segmenter(max_boundary_scan_characters=7).segment(parsed, config)

    assert error.value.code == "SEGMENTATION_LIMIT_EXCEEDED"
    assert len(Segmenter(max_boundary_scan_characters=8).segment(parsed, config).segments) == 4


def test_boundary_scan_budget_is_cumulative_across_parent_child_children():
    """所有父分段的子级必须消耗同一请求内共用的边界扫描预算。"""
    parsed = parsed_document(
        ParsedBlock("paragraph", "abc|def", {"line_start": 1}),
        ParsedBlock("paragraph", "abc|def", {"line_start": 3}),
    )
    config = ParentChildSegmentationConfig(
        "paragraph",
        parent_max_length=7,
        child_max_length=4,
        child_overlap=0,
        separator="|",
    )

    with pytest.raises(SegmentationConfigError) as error:
        Segmenter(max_boundary_scan_characters=7).segment(parsed, config)

    assert error.value.code == "SEGMENTATION_LIMIT_EXCEEDED"
    result = Segmenter(max_boundary_scan_characters=8).segment(parsed, config)
    assert len([item for item in result.segments if item.index_type == "child"]) == 4
