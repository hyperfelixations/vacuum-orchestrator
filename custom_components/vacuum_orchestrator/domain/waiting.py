"""Why a waiting job has not started, on three axes; see dev doc "Wartegrund"."""

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from .dispatching import Ineligibility
from .rooms import Room

# Robot-neutral reasons of the job itself, most important first.
JOB_ORDER = (
    "being_edited",
    "pending_confirmation",
    "room_disabled",
    "room_area_missing",
    "room_not_released",
    "requirement_not_satisfied",
    "requirement_unknown",
    "requirement_stale",
    "rooms_in_use",
)
# Queue reasons; the start delay only leads when nothing else blocks.
QUEUE_ORDER = ("queue_ending", "queue_idle", "queue_paused", "start_delayed")
HOLD_CODES = frozenset({"being_edited", "pending_confirmation"})

# Public code of every eligibility reason, in the order a robot shows them.
ROBOT_CODES = {
    "robot_needs_attention": "robot_needs_attention",
    "robot_fault": "robot_fault",
    "robot_availability_unknown": "robot_state_unknown",
    "robot_unknown": "robot_state_unknown",
    "robot_unavailable": "robot_unavailable",
    "robot_busy": "robot_busy",
    "robot_already_executing": "robot_busy",
    "battery_below_minimum": "battery_low",
    "setting_entity_unavailable": "setting_unavailable",
    "unsupported_operation": "operation_unsupported",
    "unmapped_target": "room_unreachable",
    "unsupported_map_context": "map_mismatch",
    "unsupported_pass_count": "passes_unsupported",
    "unsupported_pass_scope": "passes_unsupported",
    "unsupported_cleaning_preference": "setting_unsupported",
    "unsupported_cancel_semantics": "robot_capability_missing",
    "insufficient_start_evidence": "robot_capability_missing",
    "insufficient_completion_evidence": "robot_capability_missing",
    "unsupported_vendor_extension": "robot_capability_missing",
}
ROBOT_ORDER = (
    *dict.fromkeys(ROBOT_CODES.values()),
    "requirement_not_satisfied",
    "requirement_unknown",
    "requirement_stale",
)


class RobotsState(StrEnum):
    """Whether some robot could take the job now, and if not, why not."""

    READY = "ready"
    NO_ROBOT_CONFIGURED = "no_robot_configured"
    NO_CAPABLE_ROBOT = "no_capable_robot"
    NO_ROBOT_READY = "no_robot_ready"


@dataclass(frozen=True, slots=True)
class Blocker:
    """One reason with the rooms, entities and robots it is about."""

    code: str
    room_ids: tuple[str, ...] = ()
    entity_ids: tuple[str, ...] = ()
    robot_ids: tuple[str, ...] = ()
    until: datetime | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class RobotBlockers:
    """Every reason of one robot; empty for a robot that could start now."""

    robot_id: str
    blockers: tuple[Blocker, ...]


@dataclass(frozen=True, slots=True)
class Robots:
    """Execution axis: capable robots with momentary reasons, the others apart."""

    state: RobotsState
    candidates: tuple[RobotBlockers, ...] = ()
    unsuitable: tuple[RobotBlockers, ...] = ()

    @property
    def primary(self) -> Blocker | None:
        """Name one reason; alternatives between robots are never merged."""
        if self.state is RobotsState.READY:
            return None
        if self.state is RobotsState.NO_ROBOT_CONFIGURED:
            return Blocker("no_robot_configured")
        if self.state is RobotsState.NO_CAPABLE_ROBOT:
            return _reach(self.unsuitable) or _shared(
                self.unsuitable, "no_capable_robot"
            )
        return _shared(self.candidates, "no_robot_ready")


@dataclass(frozen=True, slots=True)
class Waiting:
    """Job, execution and queue axes of one waiting job.

    `queue` is None for next phases, which the queue does not start.
    """

    job: tuple[Blocker, ...]
    robots: Robots
    queue: tuple[Blocker, ...] | None

    @property
    def primary(self) -> Blocker:
        """Return the reason a person should resolve first."""
        if self.job:
            return self.job[0]
        robot = self.robots.primary
        if robot is not None:
            return robot
        assert self.queue
        return self.queue[0]

    @property
    def pending(self) -> bool:
        """Return whether only holds, the delay or the queue keep it waiting."""
        return (
            all(item.code in HOLD_CODES for item in self.job)
            and self.robots.state is RobotsState.READY
        )


def waiting(
    job: Iterable[Blocker], robots: Robots, queue: Iterable[Blocker] | None
) -> Waiting | None:
    """Order each axis; None means the job starts now."""
    ordered_job = _ordered(job, JOB_ORDER)
    ordered_queue = None if queue is None else _ordered(queue, QUEUE_ORDER)
    if not ordered_job and robots.state is RobotsState.READY and not ordered_queue:
        return None
    return Waiting(ordered_job, robots, ordered_queue)


def pending(value: Waiting | None) -> bool:
    """Return whether a job starts by itself once its hold and delay pass."""
    return value is None or value.pending


def robot_blocker(reason: Ineligibility) -> Blocker:
    """Name one eligibility reason by its public code."""
    return Blocker(
        ROBOT_CODES[reason.code], room_ids=reason.room_ids, detail=reason.detail
    )


def robot_blockers(robot_id: str, blockers: Iterable[Blocker]) -> RobotBlockers:
    """Order one robot's reasons for display."""
    return RobotBlockers(robot_id, _ordered(blockers, ROBOT_ORDER))


def room_blocker_code(room: Room) -> str:
    """Name why a room admits no new job; a release alone fixes only the last."""
    if not room.enabled:
        return "room_disabled"
    if room.area_missing:
        return "room_area_missing"
    return "room_not_released"


def _ordered(
    blockers: Iterable[Blocker], order: tuple[str, ...]
) -> tuple[Blocker, ...]:
    return tuple(sorted(blockers, key=lambda item: order.index(item.code)))


def _shared(entries: tuple[RobotBlockers, ...], fallback: str) -> Blocker:
    """One robot's first reason, or the reason every robot shares, or `fallback`."""
    robot_ids = tuple(entry.robot_id for entry in entries)
    first = {entry.blockers[0] for entry in entries}
    if len(first) == 1:
        return replace(first.pop(), robot_ids=robot_ids)
    return Blocker(fallback, robot_ids=robot_ids)


def _reach(entries: tuple[RobotBlockers, ...]) -> Blocker | None:
    """Explain robots that are unsuitable only because of rooms they miss."""
    if not entries or any(
        item.code != "room_unreachable" for entry in entries for item in entry.blockers
    ):
        return None
    missed = [
        tuple(room for item in entry.blockers for room in item.room_ids)
        for entry in entries
    ]
    robot_ids = tuple(entry.robot_id for entry in entries)
    everywhere = tuple(
        room for room in missed[0] if all(room in rooms for rooms in missed)
    )
    if everywhere:
        return Blocker("room_unreachable", room_ids=everywhere, robot_ids=robot_ids)
    return Blocker(
        "rooms_not_reachable_together",
        room_ids=tuple(dict.fromkeys(room for rooms in missed for room in rooms)),
        robot_ids=robot_ids,
    )
