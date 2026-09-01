from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Final
from zipfile import BadZipFile, ZipFile

from fastapi import UploadFile

from rag_modules.config.settings import UploadSettings
from rag_modules.documents.types import PreparedUpload, UploadValidationError

CHUNK_SIZE: Final = 1024 * 1024
TEXT_SAMPLE_SIZE: Final = 8192
MAX_ARCHIVE_ENTRIES: Final = 10_000
MAX_COMPRESSION_RATIO: Final = 100

MAGIC_PREFIXES: Final = {
    ".pdf": (b"%PDF-",),
    ".docx": (b"PK\x03\x04",),
    ".xlsx": (b"PK\x03\x04",),
    ".xls": (bytes.fromhex("D0CF11E0A1B11AE1"),),
}

TEXT_EXTENSIONS: Final = frozenset({".txt", ".md", ".csv"})
TEXT_CONTENT_TYPES: Final = {
    ".txt": frozenset({"text/plain"}),
    ".md": frozenset({"text/markdown", "text/plain", "text/x-markdown"}),
    ".csv": frozenset({"text/csv", "application/csv", "text/plain"}),
}


async def prepare_upload(file: UploadFile, limits: UploadSettings) -> PreparedUpload:
    """Validate an upload while hashing it in bounded-size chunks."""
    filename = file.filename or ""
    extension = Path(filename).suffix.lower()
    allowed_extensions = {item.lower() for item in limits.allowed_extensions}
    if extension not in allowed_extensions:
        raise UploadValidationError(
            "UNSUPPORTED_FILE_TYPE", "The file extension is not supported."
        )

    content_type = file.content_type or "application/octet-stream"
    _validate_text_content_type(extension, content_type)

    digest = hashlib.sha256()
    size = 0
    sample = bytearray()
    maximum_size = limits.max_file_size_mb * 1024 * 1024

    try:
        while chunk := await file.read(CHUNK_SIZE):
            size += len(chunk)
            if size > maximum_size:
                raise UploadValidationError(
                    "FILE_SIZE_LIMIT_EXCEEDED",
                    "The file exceeds the configured upload size limit.",
                )
            digest.update(chunk)
            if len(sample) < TEXT_SAMPLE_SIZE:
                sample.extend(chunk[: TEXT_SAMPLE_SIZE - len(sample)])

        _validate_signature(extension, bytes(sample))
        if extension in TEXT_EXTENSIONS:
            _validate_text_sample(bytes(sample))
        if extension in {".docx", ".xlsx"}:
            _validate_zip_container(file, limits)
    finally:
        await file.seek(0)

    return PreparedUpload(
        filename=filename,
        extension=extension,
        content_type=content_type,
        size=size,
        sha256=digest.hexdigest(),
        stream=file.file,
    )


def _validate_text_content_type(extension: str, content_type: str) -> None:
    if extension not in TEXT_EXTENSIONS:
        return
    normalized_content_type = content_type.split(";", 1)[0].strip().lower()
    if normalized_content_type not in TEXT_CONTENT_TYPES[extension]:
        raise UploadValidationError(
            "UNSUPPORTED_CONTENT_TYPE", "The content type is not valid for this text file."
        )


def _validate_signature(extension: str, sample: bytes) -> None:
    prefixes = MAGIC_PREFIXES.get(extension)
    if prefixes is not None and not any(sample.startswith(prefix) for prefix in prefixes):
        raise UploadValidationError(
            "INVALID_FILE_SIGNATURE", "The file does not match its declared format."
        )


def _validate_text_sample(sample: bytes) -> None:
    if not sample:
        return
    nul_count = sample.count(b"\x00")
    non_text_controls = sum(
        byte < 32 and byte not in {9, 10, 13} for byte in sample
    )
    if nul_count * 100 > len(sample) or non_text_controls * 5 > len(sample):
        raise UploadValidationError(
            "BINARY_TEXT_CONTENT", "The text file contains binary-looking content."
        )


def _validate_zip_container(file: UploadFile, limits: UploadSettings) -> None:
    file.file.seek(0)
    try:
        with ZipFile(file.file) as archive:
            entries = archive.infolist()
    except BadZipFile as error:
        raise UploadValidationError(
            "INVALID_FILE_SIGNATURE", "The file does not contain a valid ZIP container."
        ) from error

    if len(entries) > MAX_ARCHIVE_ENTRIES:
        raise UploadValidationError(
            "ARCHIVE_ENTRY_LIMIT_EXCEEDED", "The archive has too many entries."
        )

    maximum_uncompressed_size = limits.max_decompressed_size_mb * 1024 * 1024
    total_uncompressed_size = 0
    for entry in entries:
        if entry.flag_bits & 0x1:
            raise UploadValidationError(
                "ARCHIVE_ENCRYPTED", "Encrypted archive entries are not supported."
            )
        if _is_unsafe_archive_path(entry.filename):
            raise UploadValidationError(
                "ARCHIVE_UNSAFE_PATH", "The archive contains an unsafe entry path."
            )

        total_uncompressed_size += entry.file_size
        if total_uncompressed_size > maximum_uncompressed_size:
            raise UploadValidationError(
                "ARCHIVE_EXPANSION_LIMIT_EXCEEDED",
                "The archive exceeds the configured decompressed size limit.",
            )
        if entry.file_size and (
            not entry.compress_size
            or entry.file_size > entry.compress_size * MAX_COMPRESSION_RATIO
        ):
            raise UploadValidationError(
                "ARCHIVE_COMPRESSION_RATIO_EXCEEDED",
                "The archive contains an excessively compressed entry.",
            )


def _is_unsafe_archive_path(filename: str) -> bool:
    posix_path = PurePosixPath(filename.replace("\\", "/"))
    windows_path = PureWindowsPath(filename)
    if not posix_path.parts:
        return False
    return (
        posix_path.is_absolute()
        or windows_path.is_absolute()
        or ".." in posix_path.parts
        or ":" in posix_path.parts[0]
    )
