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
        ".png",
        ".jpg",
        ".jpeg",
    }


def test_all_image_formats_and_pdf_share_one_initialized_ocr_backend(monkeypatch):
    import io
    import sys
    import types

    from pypdf import PdfWriter
    from rag_modules.config.settings import ParserSettings
    from rag_modules.parsing.base import ParseContext

    initialized = []

    def backend(**kwargs):
        initialized.append(kwargs)
        return types.SimpleNamespace(predict=lambda image: [{
            "rec_texts": ["正文"], "rec_scores": [0.99],
        }])

    image = types.SimpleNamespace(width=100, height=100)
    monkeypatch.setitem(sys.modules, "paddleocr", types.SimpleNamespace(PaddleOCR=backend))
    monkeypatch.setattr("rag_modules.parsing.factory.settings", types.SimpleNamespace(parser=ParserSettings(ocr_enabled=True)))
    monkeypatch.setattr("rag_modules.parsing.image_parser._decode_image", lambda stream, settings: image)
    monkeypatch.setattr("rag_modules.parsing.pdf_parser.PdfPageRenderer", lambda payload, settings: types.SimpleNamespace(render=lambda payload, page: image))
    get_parser_registry.cache_clear()
    try:
        registry = get_parser_registry()
        for extension in (".png", ".jpg", ".jpeg"):
            parsed = registry.parse(extension, io.BytesIO(b"image"), ParseContext("d", "image" + extension))
            assert parsed.blocks[0].text == "正文"
        writer = PdfWriter()
        writer.add_blank_page(width=72, height=72)
        stream = io.BytesIO()
        writer.write(stream)
        assert registry.parse(".pdf", stream, ParseContext("d", "scan.pdf")).blocks[0].text == "正文"
        assert len(initialized) == 1
    finally:
        get_parser_registry.cache_clear()
