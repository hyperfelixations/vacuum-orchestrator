"""Room release grants with durable one-job reservations."""

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from .errors import ConflictError, ValidationError
from .validation import identifier, instant


class ReleaseKind(StrEnum):
    """Three user-facing grant lifetimes."""

    PERMANENT = "permanent"
    ONCE = "once"
    TIMED = "timed"


@dataclass(frozen=True, slots=True)
class RoomRelease:
    """A grant generation; admitted jobs retain separate execution rights."""

    grant_id: str
    kind: ReleaseKind
    granted_at: datetime
    expires_at: datetime | None = None
    reserved_job_id: str | None = None
    consumed: bool = False

    def __post_init__(self) -> None:
        identifier(self.grant_id)
        instant(self.granted_at)
        if self.expires_at is not None:
            instant(self.expires_at)
        if (self.kind is ReleaseKind.TIMED) != (self.expires_at is not None):
            raise ValidationError("release_expiry_mismatch")
        if self.expires_at is not None and self.expires_at <= self.granted_at:
            raise ValidationError("release_expiry_not_future")
        if self.reserved_job_id is not None:
            identifier(self.reserved_job_id)
        if (
            self.reserved_job_id is not None or self.consumed
        ) and self.kind is not ReleaseKind.ONCE:
            raise ValidationError("release_reservation_requires_once")
        if self.consumed and self.reserved_job_id is None:
            raise ValidationError("consumed_release_requires_job")

    def allows_new_job(self, now: datetime) -> bool:
        """Evaluate admission; expiry never revokes an admitted execution."""
        instant(now)
        return (
            now >= self.granted_at
            and not self.consumed
            and self.reserved_job_id is None
            and (self.expires_at is None or now < self.expires_at)
        )

    def reserve(self, job_id: str, now: datetime) -> RoomRelease:
        """Reserve an unused one-shot before releasing the command lock."""
        identifier(job_id)
        if self.reserved_job_id == job_id and not self.consumed:
            return self
        if not self.allows_new_job(now):
            raise ConflictError("room_not_released")
        return (
            replace(self, reserved_job_id=job_id)
            if self.kind is ReleaseKind.ONCE
            else self
        )

    def consume(self, job_id: str) -> RoomRelease:
        """Consume only the reservation belonging to an observed start."""
        if self.kind is not ReleaseKind.ONCE:
            return self
        if self.reserved_job_id != job_id:
            raise ConflictError("release_reservation_mismatch")
        return self if self.consumed else replace(self, consumed=True)

    def unreserve(self, job_id: str) -> RoomRelease:
        """Release only a proven unstarted reservation of this generation."""
        if self.reserved_job_id != job_id or self.consumed:
            return self
        return replace(self, reserved_job_id=None)
