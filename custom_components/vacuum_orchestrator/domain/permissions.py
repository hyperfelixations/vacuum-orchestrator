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
    from .waiting import Waiting

TERMINAL_STATES = frozenset({JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED})


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
