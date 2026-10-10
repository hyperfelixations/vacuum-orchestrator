"""What the first setup covers; see dev doc "Einrichtungsstatus"."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime

from .queue import OrchestratorState
from .reach import ReachStatus, RoomReach


@dataclass(frozen=True, slots=True)
class SetupStatus:
    """Facts for each assistant step; no step is mandatory."""

    completed_at: datetime | None
    robot_ids: tuple[str, ...]
    room_ids: tuple[str, ...]
    unreachable_room_ids: tuple[str, ...]
    defaults_configured: bool
    grace_seconds: float
    start_delay_seconds: float

    @property
    def assistant_pending(self) -> bool:
        """Return whether the assistant was never finished."""
        return self.completed_at is None


def evaluate_setup(
    state: OrchestratorState, reach: Mapping[str, Iterable[RoomReach]]
) -> SetupStatus:
    """Describe robots, active rooms and their reach, defaults and queue timing.

    `reach` maps each configured robot to its room reach.
    """
    rooms = state.room_registry.active_room_ids()
    reachable = {
        item.room_id
        for items in reach.values()
        for item in items
        if item.status is ReachStatus.REACHABLE
    }
    return SetupStatus(
        state.setup_completed_at,
        tuple(reach),
        rooms,
        tuple(room_id for room_id in rooms if room_id not in reachable),
        state.job_defaults.configured,
        state.queue_grace_seconds,
        state.start_delay_seconds,
    )
