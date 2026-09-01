import hashlib
import io
import struct
import zipfile

import pytest
from fastapi import UploadFile
from starlette.datastructures import Headers

from rag_modules.config.settings import UploadSettings
from rag_modules.documents.types import UploadValidationError
from rag_modules.documents.validation import prepare_upload


def make_upload(filename: str, content: bytes, content_type: str = "application/octet-stream") -> UploadFile:
    return UploadFile(
        file=io.BytesIO(content),
        filename=filename,
        headers=Headers({"content-type": content_type}),
    )


def make_zip(entries: list[tuple[str, bytes]], *, encrypted: bool = False) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    data = bytearray(output.getvalue())
    if encrypted:
        central_directory = data.index(b"PK\x01\x02")
        flags_offset = central_directory + 8
        flags = struct.unpack_from("<H", data, flags_offset)[0]
        struct.pack_into("<H", data, flags_offset, flags | 0x1)
    return bytes(data)


def zip_with_declared_uncompressed_size(size: int) -> bytes:
    filename = b"payload.txt"
    payload = b"x"
    local_header = struct.pack(
        "<IHHHHHIIIHH",
        0x04034B50,
        20,
        0,
        0,
        0,
        0,
        0,
        len(payload),
        size,
        len(filename),
        0,
    )
    local_record = local_header + filename + payload
    central_header = struct.pack(
        "<IHHHHHHIIIHHHHHII",
        0x02014B50,
        20,
        20,
        0,
        0,
        0,
        0,
        0,
        len(payload),
        size,
        len(filename),
        0,
        0,
        0,
        0,
        0,
        0,
    )
    central_record = central_header + filename
    end_record = struct.pack(
        "<IHHHHIIH",
        0x06054B50,
        0,
        0,
        1,
        1,
        len(central_record),
        len(local_record),
        0,
    )
    return local_record + central_record + end_record


@pytest.mark.asyncio
async def test_prepare_upload_hashes_rewinds_and_normalizes_supported_file():
    content = b"# Guide\n\nBody"
    file = make_upload("GUIDE.MD", content, "text/markdown")

    prepared = await prepare_upload(file, UploadSettings(max_file_size_mb=1))

    assert prepared.filename == "GUIDE.MD"
    assert prepared.extension == ".md"
    assert prepared.content_type == "text/markdown"
    assert prepared.size == 13
    assert prepared.sha256 == hashlib.sha256(content).hexdigest()
    assert prepared.stream.read() == content


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["legacy.doc", "slides.pptx", "payload.exe"])
async def test_prepare_upload_rejects_unsupported_extensions(name: str):
    with pytest.raises(UploadValidationError) as error:
        await prepare_upload(make_upload(name, b"data"), UploadSettings())

    assert error.value.code == "UNSUPPORTED_FILE_TYPE"


@pytest.mark.asyncio
async def test_prepare_upload_rejects_zip_container_expansion_limit():
    file = make_upload("bomb.docx", zip_with_declared_uncompressed_size(300 * 1024 * 1024))

    with pytest.raises(UploadValidationError) as error:
        await prepare_upload(file, UploadSettings(max_decompressed_size_mb=200))

    assert error.value.code == "ARCHIVE_EXPANSION_LIMIT_EXCEEDED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "content", "content_type"),
    [
        ("note.txt", b"plain text", "text/plain"),
        ("guide.md", b"# Heading", "text/markdown"),
        ("table.csv", b"name,value\na,1", "text/csv"),
        ("manual.pdf", b"%PDF-1.7\n", "application/pdf"),
        ("document.docx", make_zip([("word/document.xml", b"<document/>")]), "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ("sheet.xlsx", make_zip([("xl/workbook.xml", b"<workbook/>")]), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        ("legacy.xls", bytes.fromhex("D0CF11E0A1B11AE1") + b"workbook", "application/vnd.ms-excel"),
    ],
)
async def test_prepare_upload_accepts_each_supported_file_type(
    filename: str, content: bytes, content_type: str
):
    prepared = await prepare_upload(make_upload(filename, content, content_type), UploadSettings())

    assert prepared.extension == filename[filename.rfind(".") :]
    assert prepared.size == len(content)


@pytest.mark.asyncio
async def test_prepare_upload_accepts_empty_text_file():
    prepared = await prepare_upload(make_upload("empty.txt", b"", "text/plain"), UploadSettings())

    assert prepared.size == 0
    assert prepared.stream.read() == b""


@pytest.mark.asyncio
async def test_prepare_upload_stops_when_stream_exceeds_configured_size_limit():
    file = make_upload("large.txt", b"x" * (1024 * 1024 + 1), "text/plain")

    with pytest.raises(UploadValidationError) as error:
        await prepare_upload(file, UploadSettings(max_file_size_mb=1))

    assert error.value.code == "FILE_SIZE_LIMIT_EXCEEDED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "content", "content_type"),
    [
        ("manual.pdf", b"not a pdf", "application/pdf"),
        ("document.docx", b"not a zip", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ("sheet.xlsx", b"not a zip", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        ("legacy.xls", b"not an OLE file", "application/vnd.ms-excel"),
    ],
)
async def test_prepare_upload_rejects_bad_fixed_format_signatures(
    filename: str, content: bytes, content_type: str
):
    with pytest.raises(UploadValidationError) as error:
        await prepare_upload(make_upload(filename, content, content_type), UploadSettings())

    assert error.value.code == "INVALID_FILE_SIGNATURE"


@pytest.mark.asyncio
async def test_prepare_upload_rejects_binary_looking_text_content():
    with pytest.raises(UploadValidationError) as error:
        await prepare_upload(make_upload("malware.csv", b"\x00" * 256, "text/csv"), UploadSettings())

    assert error.value.code == "BINARY_TEXT_CONTENT"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("archive", "expected_code"),
    [
        (make_zip([("encrypted.txt", b"x")], encrypted=True), "ARCHIVE_ENCRYPTED"),
        (make_zip([("../escape.txt", b"x")]), "ARCHIVE_UNSAFE_PATH"),
        (zip_with_declared_uncompressed_size(1024 * 1024 + 1), "ARCHIVE_COMPRESSION_RATIO_EXCEEDED"),
    ],
)
async def test_prepare_upload_rejects_unsafe_zip_metadata(archive: bytes, expected_code: str):
    with pytest.raises(UploadValidationError) as error:
        await prepare_upload(
            make_upload("document.docx", archive, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
            UploadSettings(max_decompressed_size_mb=2),
        )

    assert error.value.code == expected_code
