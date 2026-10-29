"""Due counts what was surely observed and says when that was partial (DUE-03…05)."""

from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from custom_components.vacuum_orchestrator.domain.completion import CompletionQuality
from custom_components.vacuum_orchestrator.domain.due import (
    DUE_OVERDUE_GRACE_SECONDS,
    CleaningStamp,
    DueBasis,
    DueObservation,
    DueState,
    OccupancyCounter,
    evaluate_due,
)

NOW = datetime(2026, 9, 1, tzinfo=UTC)
HOUR = 3600


def stamp(
    *,
    at: datetime = NOW,
    quality: CompletionQuality = CompletionQuality.CONFIRMED,
    occupied: float = 0,
    unknown: float = 0,
    epoch: int = 0,
    known: bool = True,
) -> CleaningStamp:
    return CleaningStamp("receipt", at, quality, epoch, occupied, unknown, known)


def test_a_room_never_cleaned_is_due_without_numbers() -> None:
    report = evaluate_due(DueBasis.CALENDAR, HOUR, None, OccupancyCounter(), NOW)
    assert (report.state, report.reason, report.interval_seconds) == (
        DueState.DUE,
        "never_cleaned",
        HOUR,
    )
    assert (report.urgency, report.overdue, report.observation) == (None, False, None)


def test_without_an_interval_nothing_is_due() -> None:
    report = evaluate_due(DueBasis.CALENDAR, None, stamp(), OccupancyCounter(), NOW)
    assert (report.state, report.urgency) == (DueState.DISABLED, None)


@pytest.mark.parametrize(
    ("elapsed", "state", "overdue_seconds", "overdue"),
    [
        (HOUR / 2, DueState.FRESH, None, False),
        (HOUR, DueState.DUE, 0, False),
        (HOUR + DUE_OVERDUE_GRACE_SECONDS - 1, DueState.DUE, 1799, False),
        (HOUR + DUE_OVERDUE_GRACE_SECONDS, DueState.DUE, 1800, True),
    ],
)
def test_a_due_room_reads_as_overdue_after_the_grace(
    elapsed: float, state: DueState, overdue_seconds: float | None, overdue: bool
) -> None:
    report = evaluate_due(
        DueBasis.CALENDAR,
        HOUR,
        stamp(),
        OccupancyCounter(),
        NOW + timedelta(seconds=elapsed),
    )
    assert (report.state, report.overdue_seconds, report.overdue) == (
        state,
        overdue_seconds,
        overdue,
    )
    assert report.urgency == elapsed / HOUR
    assert report.observation is DueObservation.COMPLETE


def test_a_derived_cleaning_makes_the_observation_partial() -> None:
    derived = stamp(quality=CompletionQuality.DERIVED)
    report = evaluate_due(
        DueBasis.CALENDAR, HOUR, derived, OccupancyCounter(), NOW + timedelta(hours=2)
    )
    assert (report.state, report.observation) == (
        DueState.DUE,
        DueObservation.PARTIAL,
    )


def test_occupied_time_without_a_gap_is_complete() -> None:
    counter = OccupancyCounter().advance(NOW, True)
    report = evaluate_due(
        DueBasis.OCCUPIED, HOUR, stamp(), counter, NOW + timedelta(minutes=30)
    )
    assert (report.state, report.reason, report.observation) == (
        DueState.FRESH,
        "occupied_interval",
        DueObservation.COMPLETE,
    )
    assert (report.elapsed_seconds, report.remaining_seconds) == (1800, 1800)
    assert report.due_at is None


def test_only_a_clock_behind_the_cleaning_leaves_the_state_unknown() -> None:
    report = evaluate_due(
        DueBasis.CALENDAR, HOUR, stamp(), OccupancyCounter(), NOW - timedelta(1)
    )
    assert (report.state, report.reason, report.urgency) == (
        DueState.UNKNOWN,
        "clock_before_completion",
        None,
    )


@given(
    occupied=st.integers(0, 48 * HOUR),
    gap=st.integers(0, 48 * HOUR),
    interval=st.integers(60, 24 * HOUR),
)
def test_a_gap_never_counts_more_than_the_watched_time(
    occupied: int, gap: int, interval: int
) -> None:
    # Whole seconds: timedelta keeps microseconds only.
    watched = OccupancyCounter().advance(NOW, True)
    end = NOW + timedelta(seconds=occupied + gap)
    gapped = watched.advance(NOW + timedelta(seconds=gap), True, gap=True)
    report = evaluate_due(DueBasis.OCCUPIED, interval, stamp(), gapped, end)
    full = evaluate_due(DueBasis.OCCUPIED, interval, stamp(), watched, end)
    assert report.elapsed_seconds is not None and full.elapsed_seconds is not None
    assert report.elapsed_seconds <= full.elapsed_seconds
    assert report.state is (
        DueState.DUE if report.elapsed_seconds >= interval else DueState.FRESH
    )
    expected = DueObservation.PARTIAL if gap else DueObservation.COMPLETE
    assert report.observation is expected
