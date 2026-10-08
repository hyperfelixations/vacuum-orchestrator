"""Why a waiting job has not started, ranked so the first blocker is the answer."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

# Priority order; see dev doc "Wartegrund". The start delay only leads alone.
PRIORITY = (
    "being_edited",
    "pending_confirmation",
    "room_not_released",
    "room_unreachable",
    "rooms_not_reachable_together",
    "operation_unsupported",
    "requirement_not_satisfied",
    "requirement_unknown",
    "requirement_stale",
    "robot_needs_attention",
    "rooms_in_use",
    "robot_busy",
    "robot_unavailable",
    "battery_low",
    "robot_unsuitable",
    "no_robot_configured",
    "queue_idle",
    "queue_paused",
    "queue_ending",
    "start_delayed",
)

# Blockers that pass by themselves or describe the queue, not the job: a job
# with only these is pending work and keeps a queue run active.
PENDING_CODES = frozenset(
    {
        "being_edited",
        "pending_confirmation",
        "queue_idle",
        "queue_paused",
        "queue_ending",
        "start_delayed",
    }
)

# Selector codes that describe one robot's state rather than the job's rooms.
ROBOT_CODES = {
    "robot_needs_attention": "robot_needs_attention",
    "robot_busy": "robot_busy",
    "robot_already_executing": "robot_busy",
    "robot_unavailable": "robot_unavailable",
    "robot_unknown": "robot_unavailable",
    "robot_availability_unknown": "robot_unavailable",
    "battery_below_minimum": "battery_low",
}


@dataclass(frozen=True, slots=True)
class Blocker:
    """One reason with the rooms, entities and robots it is about."""

    code: str
    room_ids: tuple[str, ...] = ()
    entity_ids: tuple[str, ...] = ()
    robot_ids: tuple[str, ...] = ()
    until: datetime | None = None


@dataclass(frozen=True, slots=True)
class Waiting:
    """Every blocker of one job, the most important first."""

    blockers: tuple[Blocker, ...]

    @property
    def primary(self) -> Blocker:
        """Return the blocker a person should resolve first."""
        return self.blockers[0]


def rank(blockers: Iterable[Blocker]) -> Waiting | None:
    """Order blockers by priority and merge equal codes; none means it starts."""
    merged: dict[str, Blocker] = {}
    for blocker in blockers:
        current = merged.get(blocker.code)
        merged[blocker.code] = (
            blocker
            if current is None
            else Blocker(
                blocker.code,
                _union(current.room_ids, blocker.room_ids),
                _union(current.entity_ids, blocker.entity_ids),
                _union(current.robot_ids, blocker.robot_ids),
                current.until or blocker.until,
            )
        )
    if not merged:
        return None
    return Waiting(tuple(sorted(merged.values(), key=lambda item: _order(item.code))))


def pending(waiting: Waiting | None) -> bool:
    """Return whether a job starts by itself once its hold and delay pass."""
    return waiting is None or all(
        blocker.code in PENDING_CODES for blocker in waiting.blockers
    )


def robot_blocker_code(selector_code: str) -> str:
    """Map one reaching robot's selector refusal to a waiting code."""
    return ROBOT_CODES.get(selector_code, "robot_unsuitable")


def _order(code: str) -> int:
    return PRIORITY.index(code)


def _union(first: tuple[str, ...], second: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*first, *second)))
