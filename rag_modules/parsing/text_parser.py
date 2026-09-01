from __future__ import annotations

from typing import BinaryIO

from charset_normalizer import from_bytes

from rag_modules.parsing.base import ParseContext
from rag_modules.parsing.models import DocumentParseError, ParsedBlock, ParsedDocument


def decode_text(data: bytes) -> tuple[str, str]:
    """Decode only the documented, deterministic text encodings."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    else:
        try:
            _validate_text_quality(text)
        except DocumentParseError:
            # Valid UTF-8 bytes can also be a UTF-16 byte stream. Quality
            # failure is evidence against this decode, not a terminal result.
            pass
        else:
            return text, "utf-8-sig"

    utf16 = _decode_structural_utf16(data)
    if utf16 is not None:
        return utf16

    if _has_ascii_text_structure(data):
        try:
            text = data.decode("gb18030")
        except UnicodeDecodeError:
            pass
        else:
            if _round_trips_exactly(text, "gb18030", data):
                try:
                    _validate_text_quality(text)
                except DocumentParseError:
                    pass
                else:
                    return text, "gb18030"

    _raise_uncertain_encoding()


def _decode_structural_utf16(data: bytes) -> tuple[str, str] | None:
    """Accept UTF-16 only with detector and byte-lane evidence.

    The detector must make UTF-16 its first candidate, preventing arbitrary
    legacy byte streams from being reinterpreted as UTF-16. Raw TAB/LF/CR bytes
    must occupy the proper UTF-16 byte lane; this keeps an unpaired GB18030
    newline from masquerading as a Unicode code point. Exact round-trip and
    general text-quality checks remain mandatory.
    """
    matches = list(from_bytes(data))
    if not matches:
        return None
    match = matches[0]
    encoding = (match.encoding or "").replace("-", "_").lower()
    if encoding not in {"utf_16_le", "utf_16_be"}:
        return None
    if not _has_utf16_byte_lane_evidence(data, encoding):
        return None
    text = str(match)
    if not _round_trips_exactly(text, match.encoding, data):
        return None
    try:
        _validate_text_quality(text)
    except DocumentParseError:
        return None
    return text, match.encoding


def _has_utf16_byte_lane_evidence(data: bytes, encoding: str) -> bool:
    if not data or len(data) % 2:
        return False
    control_bytes = {0x09, 0x0A, 0x0D}
    for left, right in zip(data[::2], data[1::2]):
        if left not in control_bytes and right not in control_bytes:
            continue
        if encoding == "utf_16_le" and (left != 0 or right not in control_bytes):
            return False
        if encoding == "utf_16_be" and (left not in control_bytes or right != 0):
            return False
    return True


def _has_ascii_text_structure(data: bytes) -> bool:
    return any(byte in {0x09, 0x0A, 0x0D} or 0x20 <= byte <= 0x7E for byte in data)


def _round_trips_exactly(text: str, encoding: str | None, data: bytes) -> bool:
    if not encoding:
        return False
    try:
        return text.encode(encoding) == data
    except UnicodeError:
        return False


def _validate_text_quality(text: str) -> None:
    if "\ufffd" in text:
        _raise_uncertain_encoding()
    visible = [character for character in text if not character.isspace()]
    if not visible:
        return
    nul_count = text.count("\x00")
    harmful_controls = sum(
        (ord(character) < 32 and character not in "\n\r\t\x00")
        or 127 <= ord(character) < 160
        for character in visible
    )
    if harmful_controls / len(visible) > 0.05:
        _raise_uncertain_encoding()
    if nul_count > 1 and nul_count / max(len(text), 1) > 0.01:
        _raise_uncertain_encoding()


def _raise_uncertain_encoding() -> None:
    raise DocumentParseError(
        "TEXT_ENCODING_UNCERTAIN",
        "The document encoding could not be determined reliably.",
    )


def normalize_text(text: str) -> str:
    """Make source line positions stable across operating systems."""
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")


class TextParser:
    source_type = "text"

    def parse(self, stream: BinaryIO, context: ParseContext) -> ParsedDocument:
        text, encoding = decode_text(stream.read())
        blocks = tuple(_paragraph_blocks(normalize_text(text)))
        if not blocks:
            raise DocumentParseError(
                "NO_EXTRACTABLE_TEXT", "The document contains no extractable text."
            )
        return ParsedDocument(
            document_id=context.document_id,
            filename=context.filename,
            source_type=self.source_type,
            blocks=blocks,
            metadata={"encoding": encoding},
        )


def _paragraph_blocks(text: str) -> list[ParsedBlock]:
    blocks: list[ParsedBlock] = []
    paragraph_lines: list[str] = []
    line_start = 0

    for line_number, line in enumerate(text.split("\n"), start=1):
        if line.strip():
            if not paragraph_lines:
                line_start = line_number
            paragraph_lines.append(line)
            continue
        if paragraph_lines:
            blocks.append(
                ParsedBlock(
                    "paragraph",
                    "\n".join(paragraph_lines),
                    {"line_start": line_start, "line_end": line_number - 1},
                )
            )
            paragraph_lines = []

    if paragraph_lines:
        blocks.append(
            ParsedBlock(
                "paragraph",
                "\n".join(paragraph_lines),
                {"line_start": line_start, "line_end": line_start + len(paragraph_lines) - 1},
            )
        )
    return blocks
