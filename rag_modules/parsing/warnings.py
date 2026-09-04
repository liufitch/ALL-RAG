"""Bounded warning collection shared by parsers and response orchestration."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, Generic, Protocol, TypeVar

from rag_modules.parsing.models import ParserWarning


class WarningLike(Protocol):
    """The warning fields needed to recognize and fold truncation summaries."""

    code: str
    metadata: dict[str, Any]


_WarningT = TypeVar("_WarningT", bound=WarningLike)


class BoundedWarningCollector(Generic[_WarningT]):
    """Retain a bounded prefix and summarize warnings that do not fit."""

    def __init__(
        self,
        limit: int,
        summary_factory: Callable[[int], _WarningT],
    ) -> None:
        if limit <= 0:
            raise ValueError("limit must be positive")
        self._limit = limit
        self._summary_factory = summary_factory
        self._retained: list[_WarningT] = []
        self._omitted_count = 0

    def add(self, warning: _WarningT) -> None:
        if warning.code == "WARNINGS_TRUNCATED":
            metadata = warning.metadata
            omitted_count = (
                metadata.get("omitted_count") if isinstance(metadata, dict) else None
            )
            if (
                isinstance(omitted_count, int)
                and not isinstance(omitted_count, bool)
                and omitted_count > 0
            ):
                self._omitted_count += omitted_count
            else:
                self._omitted_count += 1
            return

        if len(self._retained) < self._limit - 1:
            self._retained.append(warning)
            return
        self._omitted_count += 1

    def extend(self, warnings: Iterable[_WarningT]) -> None:
        for warning in warnings:
            try:
                self.add(warning)
            finally:
                del warning

    def result(self) -> tuple[_WarningT, ...]:
        retained = tuple(self._retained)
        if self._omitted_count == 0:
            return retained
        return (*retained, self._summary_factory(self._omitted_count))


def parser_warning_summary(omitted_count: int) -> ParserWarning:
    """Build the safe document-level summary used by parser warning collectors."""
    return ParserWarning(
        "WARNINGS_TRUNCATED",
        "Additional warnings were omitted.",
        {"omitted_count": omitted_count},
    )
