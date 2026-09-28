"""Durable queue-run identity and the configurable idle completion window."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from .errors import ValidationError
from .validation import identifier, instant, seconds


@dataclass(frozen=True, slots=True)
class QueueRun:
    """One execution session, including pauses and work appended while running."""

    run_id: str
    started_at: datetime
    grace_seconds: float = 900
    idle_since: datetime | None = None
    completed_at: datetime | None = None

    def __post_init__(self) -> None:
        identifier(self.run_id)
        instant(self.started_at)
        seconds(self.grace_seconds)
        if self.grace_seconds > 86400:
            raise ValidationError("queue_grace_out_of_range")
        for value in (self.idle_since, self.completed_at):
            if value is not None:
                instant(value)
                if value < self.started_at:
                    raise ValidationError("queue_run_time_order")
        if self.completed_at is not None and (
            self.idle_since is None or self.completed_at < self.idle_since
        ):
            raise ValidationError("queue_run_time_order")

    @property
    def active(self) -> bool:
        """Return whether this run still owns its run-scoped room grants."""
        return self.completed_at is None

    @property
    def deadline(self) -> datetime | None:
        """Return the idle deadline only while this run is waiting to finish."""
        if not self.active or self.idle_since is None:
            return None
        return self.idle_since + timedelta(seconds=self.grace_seconds)
