"""Which actions an object allows now, and why not.

Commands check the same predicates; see dev doc "Erlaubte Aktionen".
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from .errors import ConflictError
from .queue_runs import RunPhase
from .types import JobState

if TYPE_CHECKING:
    from .queue import Job, OrchestratorState
    from .rooms import Room
    from .templates import JobTemplate
    from .waiting import Waiting

TERMINAL_STATES = frozenset({JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED})
_ROOM_IN_USE = frozenset(
    {
        JobState.DISPATCHING,
        JobState.RUNNING,
        JobState.CANCELING,
        JobState.NEEDS_ATTENTION,
    }
)


@dataclass(frozen=True, slots=True)
class Availability:
    """Whether an action is offered now; `reason` is a translated error code."""

    available: bool
    reason: str | None = None
    detail: str | None = None


AVAILABLE = Availability(True)


def unavailable(reason: str, detail: str | None = None) -> Availability:
    """Name why an action is not offered."""
    return Availability(False, reason, detail)


def require(availability: Availability) -> None:
    """Refuse a command with the reason its action is unavailable."""
    if not availability.available:
        raise ConflictError(str(availability.reason), availability.detail)


def editable(job: Job) -> Availability:
    """`update_job`: only waiting jobs change."""
    return (
        AVAILABLE if job.state is JobState.QUEUED else unavailable("job_not_editable")
    )


def deletable(job: Job) -> Availability:
    """`delete_job`: waiting jobs and history, never started work."""
    return (
        AVAILABLE
        if job.state is JobState.QUEUED or job.state in TERMINAL_STATES
        else unavailable("job_not_deletable")
    )


def movable(job: Job) -> Availability:
    """`move_job`: only waiting jobs have a queue position."""
    return AVAILABLE if job.state is JobState.QUEUED else unavailable("job_not_movable")


def retryable(job: Job) -> Availability:
    """`retry_job`: only finished jobs."""
    return (
        AVAILABLE if job.state in TERMINAL_STATES else unavailable("job_not_retryable")
    )


def holdable(job: Job) -> Availability:
    """`hold_job`: only waiting jobs."""
    return AVAILABLE if job.state is JobState.QUEUED else unavailable("job_not_waiting")


def job_actions(
    state: OrchestratorState, job: Job, now: datetime, waiting: Waiting | None
) -> Mapping[str, Availability]:
    """Offer each job action as seen by someone without a hold token."""
    hold = state.active_hold(job.job_id, now)
    held = None if hold is None else unavailable("job_held", hold.purpose.value)

    def unheld(availability: Availability) -> Availability:
        return held if availability.available and held is not None else availability

    position = state.queue.index(job.job_id) if job.job_id in state.queue else None
    return {
        "edit": unheld(editable(job)),
        "delete": unheld(deletable(job)),
        "cancel": _cancellable(job),
        "start": unheld(_startable(job, waiting)),
        "retry": retryable(job),
        "move_up": _boundary(job, position == 0, unavailable("job_at_top")),
        "move_down": _boundary(
            job, position == len(state.queue) - 1, unavailable("job_at_bottom")
        ),
    }


def queue_actions(phase: RunPhase) -> Mapping[str, Availability]:
    """Offer only queue commands that would change the run."""
    stopped = unavailable("queue_not_running")
    running = unavailable("queue_running")
    ending = unavailable("queue_ending")
    return {
        "run": AVAILABLE if phase is RunPhase.OFF else running,
        "pause": {
            RunPhase.OFF: stopped,
            RunPhase.PAUSED: unavailable("queue_paused"),
            RunPhase.ENDING: ending,
        }.get(phase, AVAILABLE),
        "resume": {
            RunPhase.OFF: stopped,
            RunPhase.PAUSED: AVAILABLE,
            RunPhase.ENDING: AVAILABLE,
        }.get(phase, running),
        "end": {RunPhase.OFF: stopped, RunPhase.ENDING: ending}.get(phase, AVAILABLE),
    }


def _cancellable(job: Job) -> Availability:
    # `cancel_job` also ends a waiting job; deleting is the action offered there.
    if job.state in {JobState.DISPATCHING, JobState.RUNNING}:
        return AVAILABLE
    if job.state is JobState.QUEUED:
        return unavailable("job_not_started")
    return unavailable("job_not_cancellable")


def _startable(job: Job, waiting: Waiting | None) -> Availability:
    # `start_job` skips the queue axis; the hold is checked as an active hold.
    if job.state is not JobState.QUEUED:
        return unavailable("job_not_waiting")
    if waiting is not None and not waiting.pending:
        return unavailable("job_not_startable")
    return AVAILABLE


def _boundary(job: Job, at_boundary: bool, boundary: Availability) -> Availability:
    availability = movable(job)
    return boundary if availability.available and at_boundary else availability


def room_in_use(state: OrchestratorState, room: Room) -> bool:
    """Return whether started or unresolved work targets the room."""
    aliases = {room.room_id, room.area_id}
    return any(
        job.state in _ROOM_IN_USE
        and any(target.area_id in aliases for target in job.intent.areas)
        for job in state.jobs.values()
    )


def room_editable(state: OrchestratorState, room: Room) -> Availability:
    """`update_room`, `disable_room`, `enable_room`: never under running work."""
    return unavailable("room_has_active_job") if room_in_use(state, room) else AVAILABLE


def room_actions(state: OrchestratorState, room: Room) -> Mapping[str, Availability]:
    """Offer each room action; enabling or disabling twice changes nothing."""
    usable = (
        AVAILABLE
        if room.enabled and not room.area_missing
        else unavailable("room_unavailable")
    )
    edit = room_editable(state, room)
    return {
        "release": usable,
        "revoke": AVAILABLE
        if room.release is not None
        else unavailable("room_not_released"),
        "edit": edit,
        "disable": edit if room.enabled else unavailable("room_disabled"),
        "enable": unavailable("room_enabled") if room.enabled else edit,
        "create_job": usable,
    }


def robot_idle(leased: bool) -> Availability:
    """`configure_robot`, `remove_robot`: never while a job owns the robot."""
    return unavailable("robot_busy") if leased else AVAILABLE


def returnable(leased: bool, blocked: bool, returns: bool | None) -> Availability:
    """`return_robot`; `returns` is None without a loaded adapter."""
    if returns is None:
        return unavailable("robot_unavailable")
    if leased:
        return unavailable("robot_already_executing")
    if blocked:
        return unavailable("robot_needs_attention")
    if not returns:
        return unavailable("return_to_dock_unsupported")
    return AVAILABLE


def robot_actions(
    *, leased: bool, blocked: bool, returns: bool | None, at_dock: bool | None
) -> Mapping[str, Availability]:
    """Offer each robot action; a docked robot is not sent home again."""
    home = returnable(leased, blocked, returns)
    return {
        "configure": robot_idle(leased),
        "rename": AVAILABLE,
        "remove": robot_idle(leased),
        "return_to_dock": unavailable("robot_at_dock")
        if home.available and at_dock
        else home,
    }


def template_actions(template: JobTemplate) -> Mapping[str, Availability]:
    """Offer each template action."""
    return {
        "create_job": instantiable(template),
        "edit": AVAILABLE,
        "remove": AVAILABLE,
        "reset_demand": AVAILABLE
        if template.demand_tokens
        else unavailable("no_suppressed_demand"),
    }


def instantiable(template: JobTemplate) -> Availability:
    """`create_job_from_template`: only enabled templates."""
    return AVAILABLE if template.enabled else unavailable("template_disabled")
