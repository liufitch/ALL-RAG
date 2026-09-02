from rag_modules.parsing.factory import get_parser_registry


def test_factory_registers_all_approved_extensions():
    assert get_parser_registry().registered_extensions == {
        ".txt",
        ".md",
        ".pdf",
        ".docx",
        ".xls",
        ".xlsx",
        ".csv",
    }
