"""What a person has to act on now; see dev doc "Aufmerksamkeit"."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from .types import OperationKind


class AttentionKind(StrEnum):
    """Why a person has to act."""

    DEVICE_FAULT = "device_fault"
    ROBOT_RECOVERY = "robot_recovery"
    JOB_RECOVERY = "job_recovery"


@dataclass(frozen=True, slots=True)
class Attention:
    """One thing to act on; `operations` are those a device fault stops.

    `fails_at` is when the robot's running job fails unless the fault clears.
    """

    kind: AttentionKind
    robot_id: str | None
    job_id: str | None
    codes: tuple[str, ...]
    operations: frozenset[OperationKind] = frozenset()
    since: datetime | None = None
    fails_at: datetime | None = None
