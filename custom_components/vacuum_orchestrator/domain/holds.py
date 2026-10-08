"""Leases that keep a waiting job from starting while a person decides about it."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from .errors import ValidationError
from .validation import identifier, instant

# A card renews every 30 seconds; see dev doc "Bearbeitungsschutz".
HOLD_LEASE_SECONDS = 90


class HoldPurpose(StrEnum):
    """Why a person holds a job: editing it or confirming its deletion."""

    EDIT = "edit"
    CONFIRM = "confirm"


@dataclass(frozen=True, slots=True)
class JobHold:
    """One holder's lease; only the holder knows `hold_id`."""

    hold_id: str
    job_id: str
    purpose: HoldPurpose
    acquired_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        identifier(self.hold_id)
        identifier(self.job_id)
        instant(self.acquired_at)
        instant(self.expires_at)
        if self.expires_at <= self.acquired_at:
            raise ValidationError("invalid_job_hold")

    def active(self, now: datetime) -> bool:
        """Return whether the lease still protects the job."""
        return now < self.expires_at

    def renewed(self, now: datetime) -> JobHold:
        """Extend the lease by one full period from now."""
        return JobHold(
            self.hold_id,
            self.job_id,
            self.purpose,
            self.acquired_at,
            lease_end(now),
        )


def lease_end(now: datetime) -> datetime:
    """Return when a lease acquired or renewed now expires."""
    return now + timedelta(seconds=HOLD_LEASE_SECONDS)
