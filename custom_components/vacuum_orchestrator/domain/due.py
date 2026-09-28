"""Calendar and occupied-time due evaluation with explicit observation gaps."""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum

from .completion import CompletionQuality
from .errors import ValidationError
from .validation import identifier, instant, seconds


class DueBasis(StrEnum):
    """Selectable per-room aging model."""

    CALENDAR = "calendar"
    OCCUPIED = "occupied"


class DueState(StrEnum):
    """Aging result independent of room release and robot availability."""

    DISABLED = "disabled"
    FRESH = "fresh"
    DUE = "due"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class DuePolicy:
    """Separate operation intervals sharing one configured aging basis."""

    basis: DueBasis = DueBasis.CALENDAR
    vacuum_seconds: float | None = None
    mop_seconds: float | None = None
    occupancy_entity_id: str | None = None
    occupied_state: str = "on"
    unoccupied_state: str = "off"
    occupancy_entity_registry_id: str | None = None

    def __post_init__(self) -> None:
        for duration in (self.vacuum_seconds, self.mop_seconds):
            if duration is not None:
                seconds(duration, positive=True)
        if self.occupancy_entity_id is not None:
            identifier(self.occupancy_entity_id)
        if self.occupancy_entity_registry_id is not None:
            identifier(self.occupancy_entity_registry_id)
        if self.basis is DueBasis.OCCUPIED and self.occupancy_entity_id is None:
            raise ValidationError("occupancy_source_required")
        identifier(self.occupied_state)
        identifier(self.unoccupied_state)
        if self.occupied_state == self.unoccupied_state or {
            self.occupied_state,
            self.unoccupied_state,
        } & {"unknown", "unavailable"}:
            raise ValidationError("invalid_occupancy_states")


@dataclass(frozen=True, slots=True)
class OccupancyCounter:
    """Cumulative measured occupancy and unknown time on an explicit epoch."""

    epoch: int = 0
    occupied_seconds: float = 0
    unknown_seconds: float = 0
    observed_at: datetime | None = None
    occupied: bool | None = None

    def __post_init__(self) -> None:
        if isinstance(self.epoch, bool) or self.epoch < 0:
            raise ValidationError("invalid_occupancy_epoch")
        seconds(self.occupied_seconds)
        seconds(self.unknown_seconds)
        if self.observed_at is not None:
            instant(self.observed_at)

    def advance(
        self, now: datetime, occupied: bool | None, *, gap: bool = False
    ) -> OccupancyCounter:
        """Integrate the previous observation, or mark restart time unknown."""
        instant(now)
        if self.observed_at is not None and now < self.observed_at:
            raise ValidationError("non_monotonic_observation")
        elapsed = (
            0 if self.observed_at is None else (now - self.observed_at).total_seconds()
        )
        previous = None if gap else self.occupied
        return replace(
            self,
            occupied_seconds=self.occupied_seconds
            + (elapsed if previous is True else 0),
            unknown_seconds=self.unknown_seconds + (elapsed if previous is None else 0),
            observed_at=now,
            occupied=occupied,
        )


@dataclass(frozen=True, slots=True)
class CleaningStamp:
    """Latest receipt projection and its occupied-time reference point."""

    receipt_id: str
    completed_at: datetime
    quality: CompletionQuality
    occupancy_epoch: int
    occupied_seconds: float
    unknown_seconds: float
    occupancy_baseline_known: bool = True

    def __post_init__(self) -> None:
        identifier(self.receipt_id)
        instant(self.completed_at)
        seconds(self.occupied_seconds)
        seconds(self.unknown_seconds)
        if self.occupancy_epoch < 0:
            raise ValidationError("invalid_occupancy_epoch")


@dataclass(frozen=True, slots=True)
class DueReport:
    """Read-only evaluation with no invented occupied-time due date."""

    state: DueState
    reason: str
    elapsed_seconds: float | None = None
    remaining_seconds: float | None = None
    due_at: datetime | None = None
    quality: CompletionQuality | None = None


def evaluate_due(
    basis: DueBasis,
    interval: float | None,
    stamp: CleaningStamp | None,
    counter: OccupancyCounter,
    now: datetime,
) -> DueReport:
    """Use measured lower bounds; gaps cannot conceal an already due room."""
    instant(now)
    if interval is None:
        return DueReport(DueState.DISABLED, "interval_disabled")
    if stamp is None:
        return DueReport(DueState.DUE, "never_cleaned")
    if now < stamp.completed_at:
        return DueReport(
            DueState.UNKNOWN, "clock_before_completion", quality=stamp.quality
        )
    if basis is DueBasis.CALENDAR:
        elapsed = (now - stamp.completed_at).total_seconds()
        return DueReport(
            DueState.DUE if elapsed >= interval else DueState.FRESH,
            "calendar_interval",
            elapsed,
            max(0, interval - elapsed),
            stamp.completed_at + timedelta(seconds=interval),
            stamp.quality,
        )
    if stamp.occupancy_epoch != counter.epoch or not stamp.occupancy_baseline_known:
        return DueReport(
            DueState.UNKNOWN, "occupancy_baseline_unknown", quality=stamp.quality
        )
    current = counter.advance(now, counter.occupied)
    elapsed = max(0, current.occupied_seconds - stamp.occupied_seconds)
    gap = current.unknown_seconds > stamp.unknown_seconds
    state = (
        DueState.DUE
        if elapsed >= interval
        else DueState.UNKNOWN
        if gap
        else DueState.FRESH
    )
    return DueReport(
        state,
        "occupancy_gap" if gap else "occupied_interval",
        elapsed,
        max(0, interval - elapsed),
        quality=stamp.quality,
    )
