from __future__ import annotations

from collections.abc import Sequence


def format_table_row(headers: Sequence[str], values: Sequence[str]) -> str:
    """Render a table row as self-describing header/value pairs.

    This deliberately small boundary is shared by document parsers that have
    a first-row header. Missing or blank headers receive a stable positional
    label so source values remain understandable after extraction.
    """
    pairs: list[str] = []
    for index, value in enumerate(values, start=1):
        header = headers[index - 1].strip() if index <= len(headers) else ""
        label = header or f"列{index}"
        pairs.append(f"{label}：{value.strip()}")
    return "；".join(pairs)
