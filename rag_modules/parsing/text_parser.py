from __future__ import annotations

from typing import BinaryIO

from charset_normalizer import from_bytes

from rag_modules.parsing.base import ParseContext
from rag_modules.parsing.models import DocumentParseError, ParsedBlock, ParsedDocument


def decode_text(data: bytes) -> tuple[str, str]:
    """Decode a text payload without silently accepting likely binary data."""
    try:
        return data.decode("utf-8-sig"), "utf-8-sig"
    except UnicodeDecodeError:
        pass

    matches = list(from_bytes(data))
    match = _select_match(matches, data)
    if match is None:
        raise DocumentParseError(
            "TEXT_ENCODING_UNCERTAIN",
            "The document encoding could not be determined reliably.",
        )

    text = str(match)
    if _is_chaotic_text(text, match.percent_chaos):
        raise DocumentParseError(
            "TEXT_ENCODING_UNCERTAIN",
            "The document encoding could not be determined reliably.",
        )
    return text, match.encoding


def _select_match(matches, data: bytes):
    """Return charset-normalizer's best usable match.

    Charset normalizer can classify short GB18030 text as BOM-less UTF-16 because
    both byte streams are structurally valid. UTF-16 and UTF-32 require a BOM at
    this boundary; when their best candidate is unusable, prefer a Han-script
    candidate from the same detector result.
    """
    if not matches:
        return None

    best = matches[0]
    if not _is_bomless_utf_encoding(best.encoding, data):
        return best

    han_candidates = [
        match
        for match in matches
        if not _is_bomless_utf_encoding(match.encoding, data)
        and _han_characters(str(match)) > 0
    ]
    if han_candidates:
        return max(han_candidates, key=lambda match: _han_characters(str(match)))
    return None


def _is_bomless_utf_encoding(encoding: str | None, data: bytes) -> bool:
    normalized = (encoding or "").replace("_", "-").lower()
    if normalized not in {"utf-16-be", "utf-16-le", "utf-32-be", "utf-32-le"}:
        return False
    return not data.startswith((b"\xff\xfe", b"\xfe\xff", b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff"))


def _han_characters(text: str) -> int:
    return sum("\u4e00" <= character <= "\u9fff" for character in text)


def _is_chaotic_text(text: str, chaos: float) -> bool:
    if chaos > 20 or "\ufffd" in text:
        return True
    visible = [character for character in text if not character.isspace()]
    if not visible:
        return False
    controls = sum(ord(character) < 32 or 127 <= ord(character) < 160 for character in visible)
    return controls / len(visible) > 0.05


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
