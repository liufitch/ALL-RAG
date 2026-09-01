from __future__ import annotations

from typing import Any, BinaryIO

import yaml
from markdown_it import MarkdownIt

from rag_modules.parsing.base import ParseContext
from rag_modules.parsing.models import DocumentParseError, ParsedBlock, ParsedDocument
from rag_modules.parsing.text_parser import decode_text, normalize_text


class MarkdownParser:
    source_type = "markdown"

    def __init__(self) -> None:
        self._markdown = MarkdownIt("commonmark", {"html": False}).enable("table")

    def parse(self, stream: BinaryIO, context: ParseContext) -> ParsedDocument:
        text, encoding = decode_text(stream.read())
        source, front_matter, line_offset = _extract_front_matter(normalize_text(text))
        blocks, links = _walk_tokens(self._markdown.parse(source), line_offset)
        if not blocks:
            raise DocumentParseError(
                "NO_EXTRACTABLE_TEXT", "The document contains no extractable text."
            )
        metadata: dict[str, Any] = {"encoding": encoding}
        if front_matter is not None:
            metadata.update(front_matter)
        if links:
            metadata["links"] = links
        return ParsedDocument(
            document_id=context.document_id,
            filename=context.filename,
            source_type=self.source_type,
            blocks=tuple(blocks),
            metadata=metadata,
        )


def _extract_front_matter(text: str) -> tuple[str, dict[str, Any] | None, int]:
    lines = text.split("\n")
    if not lines or lines[0] != "---":
        return text, None, 0
    for index in range(1, len(lines)):
        if lines[index] != "---":
            continue
        raw_front_matter = "\n".join(lines[1:index])
        try:
            return (
                "\n".join(lines[index + 1 :]),
                {"front_matter": yaml.safe_load(raw_front_matter)},
                index + 1,
            )
        except yaml.YAMLError:
            return (
                "\n".join(lines[index + 1 :]),
                {"front_matter_raw": raw_front_matter},
                index + 1,
            )
    return text, None, 0


def _walk_tokens(tokens, line_offset: int) -> tuple[list[ParsedBlock], list[str]]:
    blocks: list[ParsedBlock] = []
    links: list[str] = []
    heading_stack: list[tuple[int, str]] = []
    list_item_depth = 0
    table_cells: list[str] | None = None
    active_heading_level: int | None = None
    active_paragraph_map: list[int] | None = None

    for token in tokens:
        if token.type == "heading_open":
            active_heading_level = int(token.tag[1:])
            active_paragraph_map = token.map
        elif token.type == "paragraph_open":
            active_paragraph_map = token.map
        elif token.type == "list_item_open":
            list_item_depth += 1
        elif token.type == "list_item_close":
            list_item_depth -= 1
        elif token.type == "tr_open":
            table_cells = []
            active_paragraph_map = token.map
        elif token.type == "tr_close":
            if table_cells is not None:
                _append_block(
                    blocks,
                    "table_row",
                    " | ".join(table_cells),
                    heading_stack,
                    active_paragraph_map,
                    line_offset,
                )
            table_cells = None
        elif token.type == "fence":
            language = token.info.strip().split(maxsplit=1)[0] if token.info.strip() else None
            metadata = _block_metadata(heading_stack, token.map, line_offset)
            if language:
                metadata["language"] = language
            if token.content:
                blocks.append(ParsedBlock("code", token.content, metadata))
        elif token.type == "inline":
            text = _inline_text(token, links)
            if table_cells is not None:
                table_cells.append(text)
            elif active_heading_level is not None:
                heading_stack = [
                    entry for entry in heading_stack if entry[0] < active_heading_level
                ]
                heading_stack.append((active_heading_level, text))
                _append_block(
                    blocks,
                    "heading",
                    text,
                    heading_stack,
                    active_paragraph_map,
                    line_offset,
                )
                active_heading_level = None
            elif text.strip():
                _append_block(
                    blocks,
                    "list_item" if list_item_depth else "paragraph",
                    text,
                    heading_stack,
                    active_paragraph_map,
                    line_offset,
                )

    return blocks, links


def _inline_text(token, links: list[str]) -> str:
    pieces: list[str] = []
    for child in token.children or ():
        if child.type in {"link_open", "image"}:
            destination = child.attrs.get("href") or child.attrs.get("src")
            if destination and destination not in links:
                links.append(destination)
        if child.type in {"softbreak", "hardbreak"}:
            pieces.append("\n")
        elif child.type not in {"link_open", "link_close"}:
            pieces.append(child.content)
    return "".join(pieces)


def _append_block(
    blocks: list[ParsedBlock],
    block_type: str,
    text: str,
    heading_stack: list[tuple[int, str]],
    source_map: list[int] | None,
    line_offset: int,
) -> None:
    if text.strip():
        blocks.append(
            ParsedBlock(
                block_type,
                text,
                _block_metadata(heading_stack, source_map, line_offset),
            )
        )


def _block_metadata(
    heading_stack: list[tuple[int, str]], source_map: list[int] | None, line_offset: int
) -> dict[str, Any]:
    metadata: dict[str, Any] = {"heading_path": [title for _, title in heading_stack]}
    if source_map:
        metadata["line_start"] = source_map[0] + 1 + line_offset
        metadata["line_end"] = source_map[1] + line_offset
    return metadata
