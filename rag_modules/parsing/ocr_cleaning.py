from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata
from typing import Iterable

from rag_modules.config.settings import ParserSettings
from rag_modules.parsing.models import ParsedBlock
from rag_modules.parsing.ocr import OcrLine


@dataclass(frozen=True)
class CleanedOcrPage:
    blocks: tuple[ParsedBlock, ...]
    filtered_line_count: int
    repeated_header_footer_removed: bool


def clean_ocr_lines(
    lines: Iterable[OcrLine],
    *,
    page_number: int,
    settings: ParserSettings,
    repeated_lines: set[str],
) -> CleanedOcrPage:
    retained: list[tuple[str, float, tuple[tuple[float, float], ...] | None]] = []
    filtered_count = 0
    repeated_removed = False
    confidence_floor = getattr(settings, "ocr_min_confidence", 0.5)
    repeated_normalized = {normalize_ocr_text(text) for text in repeated_lines}

    normalized_lines = tuple((normalize_ocr_text(line.text), line) for line in lines)
    candidates: list[tuple[str, OcrLine]] = []
    for text, line in normalized_lines:
        if not text or _is_punctuation_noise(text) or line.confidence < confidence_floor:
            filtered_count += 1
            continue
        candidates.append((text, line))

    for index, (text, line) in enumerate(candidates):
        is_boundary = index == 0 or index == len(candidates) - 1
        if is_boundary and text in repeated_normalized and len(text) <= 80:
            filtered_count += 1
            repeated_removed = True
            continue
        retained.append((text, line.confidence, line.box))

    if not retained:
        return CleanedOcrPage((), filtered_count, repeated_removed)

    paragraphs: list[list[tuple[str, float, tuple[tuple[float, float], ...] | None]]] = []
    for item in retained:
        if paragraphs and _has_visible_paragraph_gap(paragraphs[-1][-1][2], item[2]):
            paragraphs.append([])
        if not paragraphs:
            paragraphs.append([])
        paragraphs[-1].append(item)

    blocks = tuple(
        _paragraph_block(paragraph, page_number)
        for paragraph in paragraphs
        if paragraph
    )
    return CleanedOcrPage(blocks, filtered_count, repeated_removed)


def _paragraph_block(lines, page_number: int) -> ParsedBlock:
    text = " ".join(item[0] for item in lines)
    confidence = round(sum(item[1] for item in lines) / len(lines), 4)
    boxes = [
        [[round(x, 2), round(y, 2)] for x, y in box]
        for _, _, box in lines[:100]
        if box is not None
    ]
    metadata = {"page": page_number, "ocr": True, "confidence": confidence}
    if boxes:
        metadata["boxes"] = boxes
        if sum(1 for _, _, box in lines if box is not None) > len(boxes):
            metadata["boxes_truncated"] = True
    return ParsedBlock("paragraph", text, metadata)


def _has_visible_paragraph_gap(previous_box, current_box) -> bool:
    if previous_box is None or current_box is None:
        return False
    previous_top = min(point[1] for point in previous_box)
    previous_bottom = max(point[1] for point in previous_box)
    current_top = min(point[1] for point in current_box)
    current_bottom = max(point[1] for point in current_box)
    previous_height = previous_bottom - previous_top
    current_height = current_bottom - current_top
    gap = current_top - previous_bottom
    return gap > max(previous_height, current_height) * 0.75


def normalize_ocr_text(value: str) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    safe_characters = (
        character
        for character in normalized
        if character in "\n\t" or unicodedata.category(character)[0] != "C"
    )
    return re.sub(r"\s+", " ", "".join(safe_characters)).strip()


def _is_punctuation_noise(value: str) -> bool:
    visible = [character for character in value if not character.isspace()]
    return bool(visible) and all(
        unicodedata.category(character).startswith("P") for character in visible
    )
