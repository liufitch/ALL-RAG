from __future__ import annotations

from typing import BinaryIO

from charset_normalizer import from_bytes

from rag_modules.parsing.base import ParseContext
from rag_modules.parsing.models import DocumentParseError, ParsedBlock, ParsedDocument


def decode_text(data: bytes) -> tuple[str, str]:
    """Decode a text payload without silently accepting likely binary data."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    else:
        _validate_text_quality(text)
        return text, "utf-8-sig"

    matches = list(from_bytes(data))
    match = _select_match(matches, data)
    if match is None:
        raise DocumentParseError(
            "TEXT_ENCODING_UNCERTAIN",
            "The document encoding could not be determined reliably.",
        )
    return str(match), match.encoding


def _select_match(matches, data: bytes):
    """Choose a detector candidate only when it is an exact, plausible decode.

    Charset-normalizer's candidates are already ordered by its confidence model.
    We retain candidates that round-trip exactly, stay below its chaos threshold,
    pass text quality checks, and do not show implausible script alternation. The
    final score honours coherence first, then lower chaos, then detector order.
    This accepts a genuine BOM-less UTF-16 result while avoiding a language-
    specific encoding guess for a short legacy payload.
    """
    # A few non-UTF bytes have too little evidence for charset detection: many
    # single- and double-byte encodings can round-trip them into unrelated text.
    if len(data) < 6:
        return None

    candidates = []
    for index, match in enumerate(matches):
        text = str(match)
        if not _round_trips_exactly(text, match.encoding, data):
            continue
        if match.percent_chaos > 20 or not _is_plausible_text(text):
            continue
        candidates.append((match, index))

    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: (
            item[0].percent_coherence,
            -item[0].percent_chaos,
            -item[1],
        ),
    )[0]


def _round_trips_exactly(text: str, encoding: str | None, data: bytes) -> bool:
    if not encoding:
        return False
    try:
        return text.encode(encoding) == data
    except UnicodeError:
        return False


def _is_plausible_text(text: str) -> bool:
    if len([character for character in text if not character.isspace()]) < 3:
        return False
    try:
        _validate_text_quality(text)
    except DocumentParseError:
        return False
    return not _has_implausible_script_transitions(text)


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


def _has_implausible_script_transitions(text: str) -> bool:
    """Reject detector outputs that alternate incompatible writing systems.

    This is deliberately a broad text-quality signal, not a preference for a
    language or an encoding. Japanese CJK/Hiragana/Katakana runs are compatible;
    a result that hops between unrelated scripts character-by-character is not.
    """
    scripts = [
        script
        for character in text
        if (script := _script_group(character)) is not None
    ]
    if not scripts:
        return False
    if "other" in scripts and len(set(scripts)) > 1:
        return True
    transitions = sum(left != right for left, right in zip(scripts, scripts[1:]))
    if {"cjk", "hangul"}.issubset(scripts) and transitions / len(scripts) > 0.2:
        return True
    return transitions / len(scripts) > 0.7


def _script_group(character: str) -> str | None:
    codepoint = ord(character)
    if character.isspace() or character.isdigit() or not character.isalpha():
        return None
    if 0x4E00 <= codepoint <= 0x9FFF:
        return "cjk"
    if 0x3040 <= codepoint <= 0x30FF or 0xFF66 <= codepoint <= 0xFF9D:
        return "japanese"
    if 0xAC00 <= codepoint <= 0xD7AF:
        return "hangul"
    if character.isascii():
        return "latin"
    return "other"


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
