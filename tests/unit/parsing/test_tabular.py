import io
from pathlib import Path

import pytest

from rag_modules.config.settings import settings
from rag_modules.parsing.base import ParseContext
from rag_modules.parsing.csv_parser import CsvParser
from rag_modules.parsing.models import DocumentParseError
from rag_modules.parsing.registry import ParserRegistry
from rag_modules.parsing.xls_parser import XlsParser
from rag_modules.parsing.xlsx_parser import XlsxParser


FIXTURE_DIRECTORY = Path(__file__).parents[2] / "fixtures" / "documents"


@pytest.fixture
def fixture_bytes():
    def load(name: str) -> bytes:
        return (FIXTURE_DIRECTORY / name).read_bytes()

    return load


@pytest.fixture
def parser_registry() -> ParserRegistry:
    return ParserRegistry(
        {".xls": XlsParser(), ".xlsx": XlsxParser(), ".csv": CsvParser()}
    )


@pytest.mark.parametrize("fixture_name", ["orders.xls", "orders.xlsx", "orders.csv"])
def test_tabular_parsers_emit_self_describing_rows(
    fixture_name, parser_registry, fixture_bytes
):
    """Dropping headers or source rows makes table facts ambiguous during indexing."""
    parsed = parser_registry.parse(
        Path(fixture_name).suffix,
        io.BytesIO(fixture_bytes(fixture_name)),
        ParseContext("d", fixture_name),
    )

    row = next(block for block in parsed.blocks if block.block_type == "table_row")
    assert row.text == "订单号：A001；客户：张三；金额：100"
    assert row.metadata == {
        "sheet": "CSV" if fixture_name.endswith(".csv") else "订单",
        "row": 2,
        "headers": ["订单号", "客户", "金额"],
    }


def test_xlsx_uses_sheet_name_skips_hidden_sheets_and_ignores_empty_sheets(
    parser_registry, fixture_bytes
):
    """Reading hidden or empty sheets would expose non-user table content as facts."""
    parsed = parser_registry.parse(
        ".xlsx",
        io.BytesIO(fixture_bytes("multi-sheet.xlsx")),
        ParseContext("d", "x.xlsx"),
    )

    assert {block.metadata["sheet"] for block in parsed.blocks} == {"订单", "客户"}
    assert any(warning.code == "HIDDEN_SHEET_SKIPPED" for warning in parsed.warnings)
    assert all(block.metadata["sheet"] != "空白" for block in parsed.blocks)


def test_tabular_normalization_keeps_column_alignment_and_formula_text(
    parser_registry, fixture_bytes
):
    """Coercing nulls or cached formula values would pair facts with the wrong headers."""
    parsed = parser_registry.parse(
        ".xlsx",
        io.BytesIO(fixture_bytes("normalization.xlsx")),
        ParseContext("d", "normalization.xlsx"),
    )

    assert [(block.text, block.metadata) for block in parsed.blocks] == [
        (
            "日期：2026-08-31；开关：true；金额：100；列4：保留；名称_2：=SUM(1,2)",
            {
                "sheet": "数据",
                "row": 2,
                "headers": ["日期", "开关", "金额", "列4", "名称", "名称_2"],
            },
        )
    ]


def test_csv_uses_sniffed_semicolon_dialect_and_true_line_number_after_quoted_newline(
    parser_registry, fixture_bytes
):
    """Using record indexes would mislocate CSV data following a quoted newline."""
    parsed = parser_registry.parse(
        ".csv",
        io.BytesIO(fixture_bytes("quoted-newline.csv")),
        ParseContext("d", "quoted-newline.csv"),
    )

    assert [(block.text, block.metadata) for block in parsed.blocks] == [
        (
            "名称：第一\n行；备注：ok",
            {"sheet": "CSV", "row": 3, "headers": ["名称", "备注"]},
        ),
        (
            "名称：第二行；备注：done",
            {"sheet": "CSV", "row": 4, "headers": ["名称", "备注"]},
        ),
    ]


def test_csv_falls_back_deterministically_for_a_short_single_column_file(
    parser_registry, fixture_bytes
):
    """Letting Sniffer failure escape would reject valid one-column CSV uploads."""
    parsed = parser_registry.parse(
        ".csv",
        io.BytesIO(fixture_bytes("short-single-column.csv")),
        ParseContext("d", "single.csv"),
    )

    assert [(block.text, block.metadata) for block in parsed.blocks] == [
        ("name：alice", {"sheet": "CSV", "row": 2, "headers": ["name"]})
    ]


def test_empty_xlsx_has_no_extractable_table_rows(parser_registry, fixture_bytes):
    """Treating an empty sheet as a data row would fabricate searchable table content."""
    with pytest.raises(DocumentParseError) as error:
        parser_registry.parse(
            ".xlsx",
            io.BytesIO(fixture_bytes("empty.xlsx")),
            ParseContext("d", "empty.xlsx"),
        )

    assert error.value.code == "NO_EXTRACTABLE_TEXT"


def test_xls_normalizes_blank_cells_and_booleans_without_emitting_empty_rows(
    parser_registry, fixture_bytes
):
    """Passing xlrd blank/boolean primitives through creates false table facts."""
    parsed = parser_registry.parse(
        ".xls",
        io.BytesIO(fixture_bytes("legacy-null-bool.xls")),
        ParseContext("d", "legacy-null-bool.xls"),
    )

    assert [(block.text, block.metadata) for block in parsed.blocks] == [
        (
            "左：A；右：C；启用：true",
            {"sheet": "旧表", "row": 2, "headers": ["左", "中", "右", "启用"]},
        )
    ]


def test_csv_normalizes_empty_fields_and_ignores_a_fully_empty_data_row(
    parser_registry, fixture_bytes
):
    """Treating CSV empty fields as text emits empty pairs and a fake data record."""
    parsed = parser_registry.parse(
        ".csv",
        io.BytesIO(fixture_bytes("null-rows.csv")),
        ParseContext("d", "null-rows.csv"),
    )

    assert [(block.text, block.metadata) for block in parsed.blocks] == [
        (
            "左：A；右：C",
            {"sheet": "CSV", "row": 2, "headers": ["左", "中", "右"]},
        )
    ]


def test_ragged_csv_extends_and_deduplicates_headers_before_any_block_metadata(
    parser_registry, fixture_bytes
):
    """Finalizing ragged rows early leaves metadata unable to describe emitted columns."""
    parsed = parser_registry.parse(
        ".csv",
        io.BytesIO(fixture_bytes("ragged.csv")),
        ParseContext("d", "ragged.csv"),
    )

    assert [(block.text, block.metadata) for block in parsed.blocks] == [
        (
            "甲：A；列3：B；列3_2：C",
            {"sheet": "CSV", "row": 2, "headers": ["甲", "列3", "列3_2"]},
        )
    ]


def test_row_limit_is_a_document_budget_that_counts_visible_sheet_headers(
    monkeypatch, parser_registry, fixture_bytes
):
    """Resetting the limit per sheet permits a workbook to exceed its document budget."""
    monkeypatch.setattr(settings.parser, "max_rows", 3)

    with pytest.raises(DocumentParseError) as error:
        parser_registry.parse(
            ".xlsx",
            io.BytesIO(fixture_bytes("row-budget.xlsx")),
            ParseContext("d", "row-budget.xlsx"),
        )

    assert error.value.code == "TABLE_ROW_LIMIT_EXCEEDED"


def test_csv_row_budget_counts_non_empty_header_and_data_but_not_blank_records(
    monkeypatch, parser_registry, fixture_bytes
):
    """A blank CSV record must not consume the same logical row budget as source data."""
    monkeypatch.setattr(settings.parser, "max_rows", 2)

    parsed = parser_registry.parse(
        ".csv",
        io.BytesIO(fixture_bytes("null-rows.csv")),
        ParseContext("d", "null-rows.csv"),
    )

    assert [block.metadata["row"] for block in parsed.blocks] == [2]


def test_xlsx_ignores_style_only_declared_dimensions_for_row_and_column_limits(
    monkeypatch, parser_registry, fixture_bytes
):
    """Trusting a worksheet's declared Z100 dimension rejects tiny real tables."""
    monkeypatch.setattr(settings.parser, "max_rows", 2)
    monkeypatch.setattr(settings.parser, "max_columns", 2)

    parsed = parser_registry.parse(
        ".xlsx",
        io.BytesIO(fixture_bytes("style-only-dimensions.xlsx")),
        ParseContext("d", "style-only-dimensions.xlsx"),
    )

    assert [(block.text, block.metadata["row"]) for block in parsed.blocks] == [("列：值", 2)]


def test_xlsx_remote_non_null_cell_uses_its_real_column_for_the_limit(
    monkeypatch, parser_registry, fixture_bytes
):
    """Ignoring declared dimensions must not hide a real distant value beyond the column limit."""
    monkeypatch.setattr(settings.parser, "max_rows", 2)
    monkeypatch.setattr(settings.parser, "max_columns", 2)

    with pytest.raises(DocumentParseError) as error:
        parser_registry.parse(
            ".xlsx",
            io.BytesIO(fixture_bytes("remote-value-dimensions.xlsx")),
            ParseContext("d", "remote-value-dimensions.xlsx"),
        )

    assert error.value.code == "TABLE_COLUMN_LIMIT_EXCEEDED"


@pytest.mark.parametrize(
    ("fixture_name", "limit_name", "limit", "expected_code"),
    [
        ("orders.xlsx", "max_rows", 1, "TABLE_ROW_LIMIT_EXCEEDED"),
        ("orders.xlsx", "max_columns", 2, "TABLE_COLUMN_LIMIT_EXCEEDED"),
        ("orders.csv", "max_cell_characters", 2, "TABLE_CELL_CHARACTER_LIMIT_EXCEEDED"),
    ],
)
def test_tabular_parsers_reject_configured_limits_at_the_parser_boundary(
    monkeypatch, parser_registry, fixture_bytes, fixture_name, limit_name, limit, expected_code
):
    """Silently truncating table bounds would let callers index incomplete source data."""
    monkeypatch.setattr(settings.parser, limit_name, limit)

    with pytest.raises(DocumentParseError) as error:
        parser_registry.parse(
            Path(fixture_name).suffix,
            io.BytesIO(fixture_bytes(fixture_name)),
            ParseContext("d", fixture_name),
        )

    assert error.value.code == expected_code
    assert error.value.retryable is False


@pytest.mark.parametrize(
    ("extension", "payload", "expected_code"),
    [
        (".xlsx", b"not a workbook", "XLSX_MALFORMED"),
        (".xls", b"not a workbook", "XLS_MALFORMED"),
        (".csv", bytes(range(256)) * 4, "CSV_ENCODING_UNCERTAIN"),
    ],
)
def test_tabular_parsers_normalize_unreadable_inputs_to_stable_errors(
    parser_registry, extension, payload, expected_code
):
    """Leaking reader-library exceptions would make upload failures API-unstable."""
    with pytest.raises(DocumentParseError) as error:
        parser_registry.parse(extension, io.BytesIO(payload), ParseContext("d", f"x{extension}"))

    assert error.value.code == expected_code
