from rag_modules.config.settings import ParserSettings
from rag_modules.parsing.ocr import OcrLine
from rag_modules.parsing.ocr_cleaning import clean_ocr_lines


def test_clean_ocr_lines_normalizes_controls_filters_low_confidence_and_merges_lines():
    lines = (
        OcrLine(
            "  第一行\x00  ",
            0.98,
            ((0, 0), (10, 0), (10, 10), (0, 10)),
        ),
        OcrLine("第二行", 0.96, None),
        OcrLine("噪声!!!", 0.20, None),
    )

    cleaned = clean_ocr_lines(
        lines,
        page_number=2,
        settings=ParserSettings(),
        repeated_lines=set(),
    )

    assert [block.text for block in cleaned.blocks] == ["第一行 第二行"]
    assert cleaned.filtered_line_count == 1
    assert cleaned.blocks[0].metadata["page"] == 2
    assert cleaned.blocks[0].metadata["ocr"] is True
    assert cleaned.blocks[0].metadata["confidence"] == 0.97


def test_clean_ocr_lines_removes_repeated_short_header_and_footer():
    lines = (
        OcrLine("项目内部资料", 0.99, None),
        OcrLine("正文内容", 0.99, None),
    )

    cleaned = clean_ocr_lines(
        lines,
        page_number=3,
        settings=ParserSettings(),
        repeated_lines={"项目内部资料"},
    )

    assert [block.text for block in cleaned.blocks] == ["正文内容"]
    assert cleaned.repeated_header_footer_removed is True


def test_clean_ocr_lines_keeps_repeated_text_when_it_is_in_the_body():
    lines = (
        OcrLine("正文开头", 0.99, None),
        OcrLine("项目内部资料", 0.99, None),
        OcrLine("正文结尾", 0.99, None),
    )

    cleaned = clean_ocr_lines(
        lines,
        page_number=3,
        settings=ParserSettings(),
        repeated_lines={"项目内部资料"},
    )

    assert [block.text for block in cleaned.blocks] == [
        "正文开头 项目内部资料 正文结尾"
    ]
    assert cleaned.repeated_header_footer_removed is False


def test_clean_ocr_lines_starts_a_new_paragraph_after_a_visible_vertical_gap():
    lines = (
        OcrLine("第一行", 0.99, ((0, 0), (10, 0), (10, 10), (0, 10))),
        OcrLine("第二行", 0.99, ((0, 12), (10, 12), (10, 22), (0, 22))),
        OcrLine("新段落", 0.99, ((0, 50), (10, 50), (10, 60), (0, 60))),
    )

    cleaned = clean_ocr_lines(
        lines,
        page_number=1,
        settings=ParserSettings(),
        repeated_lines=set(),
    )

    assert [block.text for block in cleaned.blocks] == ["第一行 第二行", "新段落"]
