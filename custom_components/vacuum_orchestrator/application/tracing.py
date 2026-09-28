"""Bounded structured execution traces without device payloads or free text."""

from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum


class TraceEvent(StrEnum):
    """Stable diagnostic event families."""

    JOB = "job_transition"
    ATTEMPT = "attempt_transition"
    OBSERVATION = "robot_observation"
    BLOCKED = "dispatch_blocked"
    RECOVERY = "recovery_resolved"


@dataclass(frozen=True, slots=True)
class TraceRecord:
    """Allowlisted evidence fields; no names, notes, maps or vendor objects."""

    sequence: int
    timestamp: str
    event: str
    job_id: str | None = None
    attempt_id: str | None = None
    robot_id: str | None = None
    state: str | None = None
    reason: str | None = None
    quality: str | None = None


class TraceRecorder:
    """Keep a bounded runtime trace independently of critical execution storage."""

    def __init__(self, capacity: int = 512) -> None:
        self._records: deque[TraceRecord] = deque(maxlen=capacity)
        self.sequence = 0

    def record(
        self,
        event: TraceEvent,
        now: datetime,
        *,
        job_id: str | None = None,
        attempt_id: str | None = None,
        robot_id: str | None = None,
        state: str | None = None,
        reason: str | None = None,
        quality: str | None = None,
    ) -> None:
        """Append only normalized values chosen by the application."""
        self.sequence += 1
        self._records.append(
            TraceRecord(
                self.sequence,
                now.isoformat(),
                event.value,
                job_id,
                attempt_id,
                robot_id,
                state,
                reason,
                quality,
            )
        )

    def snapshot(self, job_id: str | None = None) -> list[dict[str, str | int | None]]:
        """Return independent records, optionally scoped to one job."""
        return [
            asdict(item)
            for item in self._records
            if job_id is None or item.job_id == job_id
        ]
