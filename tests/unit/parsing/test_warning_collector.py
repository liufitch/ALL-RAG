import gc
import weakref

import pytest

from rag_modules.parsing.models import ParserWarning
from rag_modules.parsing.warnings import BoundedWarningCollector


def _warning(code: str, metadata=None) -> ParserWarning:
    return ParserWarning(code, f"message for {code}", metadata or {})


def _summary(omitted_count: int) -> ParserWarning:
    return ParserWarning(
        "WARNINGS_TRUNCATED",
        "Additional warnings were omitted.",
        {"omitted_count": omitted_count},
    )


def test_empty_collector_has_no_summary():
    collector = BoundedWarningCollector[ParserWarning](3, _summary)

    assert collector.result() == ()


def test_below_limit_preserves_every_warning_in_order():
    collector = BoundedWarningCollector[ParserWarning](4, _summary)
    warnings = (_warning("FIRST"), _warning("SECOND"), _warning("THIRD"))

    collector.extend(warnings)

    assert collector.result() == warnings


def test_exact_limit_reserves_the_last_slot_for_summary():
    collector = BoundedWarningCollector[ParserWarning](3, _summary)

    collector.extend(_warning(code) for code in ("FIRST", "SECOND", "THIRD"))

    assert [warning.code for warning in collector.result()] == [
        "FIRST",
        "SECOND",
        "WARNINGS_TRUNCATED",
    ]
    assert collector.result()[-1].metadata == {"omitted_count": 1}


def test_limit_one_returns_only_summary_with_total_omitted_count():
    collector = BoundedWarningCollector[ParserWarning](1, _summary)

    collector.extend(_warning(code) for code in ("FIRST", "SECOND", "THIRD"))

    assert collector.result() == (_summary(3),)


def test_over_limit_counts_every_warning_after_the_reserved_prefix():
    collector = BoundedWarningCollector[ParserWarning](3, _summary)

    collector.extend(_warning(str(index)) for index in range(6))

    assert [warning.code for warning in collector.result()] == [
        "0",
        "1",
        "WARNINGS_TRUNCATED",
    ]
    assert collector.result()[-1].metadata == {"omitted_count": 4}


def test_chained_extend_uses_one_shared_limit_and_omission_count():
    collector = BoundedWarningCollector[ParserWarning](3, _summary)

    collector.extend((_warning("FIRST"), _warning("SECOND")))
    collector.extend((_warning("THIRD"), _warning("FOURTH")))

    assert [warning.code for warning in collector.result()] == [
        "FIRST",
        "SECOND",
        "WARNINGS_TRUNCATED",
    ]
    assert collector.result()[-1].metadata == {"omitted_count": 2}


def test_preexisting_summary_folds_its_positive_omitted_count():
    collector = BoundedWarningCollector[ParserWarning](4, _summary)

    collector.extend((_warning("FIRST"), _summary(7), _warning("SECOND")))

    assert [warning.code for warning in collector.result()] == [
        "FIRST",
        "SECOND",
        "WARNINGS_TRUNCATED",
    ]
    assert collector.result()[-1].metadata == {"omitted_count": 7}


@pytest.mark.parametrize(
    "metadata",
    (
        {},
        {"omitted_count": 0, "source": "secret-zero"},
        {"omitted_count": -2, "source": "secret-negative"},
        {"omitted_count": True, "source": "secret-bool"},
        {"omitted_count": "2", "source": "secret-string"},
    ),
)
def test_invalid_preexisting_summary_is_suppressed_and_counted_once(metadata):
    collector = BoundedWarningCollector[ParserWarning](3, _summary)

    collector.add(_warning("WARNINGS_TRUNCATED", metadata))
    collector.add(_warning("SAFE"))

    result = collector.result()
    assert [warning.code for warning in result] == ["SAFE", "WARNINGS_TRUNCATED"]
    assert result[-1].metadata == {"omitted_count": 1}
    assert "secret" not in repr(result)


@pytest.mark.parametrize("limit", (0, -1))
def test_non_positive_limit_is_rejected(limit):
    with pytest.raises(ValueError, match="limit must be positive"):
        BoundedWarningCollector[ParserWarning](limit, _summary)


def test_extend_releases_each_omitted_warning_metadata_before_requesting_the_next():
    class TrackedMetadata(dict):
        pass

    released: list[bool] = []

    def warning_stream():
        for index in range(4):
            metadata = TrackedMetadata(index=index)
            reference = weakref.ref(metadata)
            yield _warning(f"WARNING_{index}", metadata)
            del metadata
            gc.collect()
            released.append(reference() is None)

    collector = BoundedWarningCollector[ParserWarning](1, _summary)

    collector.extend(warning_stream())

    assert released == [True, True, True, True]
    assert collector.result() == (_summary(4),)
