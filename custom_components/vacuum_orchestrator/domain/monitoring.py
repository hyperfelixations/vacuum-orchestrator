"""Pure execution monitoring; evidence quality is independent of cleaning mode."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from .completion import CompletionQuality
from .dispatching import RobotObservation
from .execution import ExecutionAttempt
from .planning import DispatchAssignment, WorkUnit
from .types import AttemptState, RobotAvailabilityState


class MonitorAction(StrEnum):
    """Transitions that the single writer may persist from an observation."""

    WAIT = "wait"
    START = "start"
    SETTLE = "settle"
    RESUME = "resume"
    COMPLETE = "complete"
    CANCEL = "cancel"
    ATTENTION = "attention"


@dataclass(frozen=True, slots=True)
class MonitorDecision:
    """One explainable transition, with quality only for successful completion."""

    action: MonitorAction
    reason: str
    quality: CompletionQuality | None = None


def next_deadline(attempt: ExecutionAttempt) -> datetime | None:
    """Return a persisted attempt's next deadline without polling devices."""
    if attempt.state is AttemptState.COMMAND_SENT:
        return (attempt.command_boundary_at or attempt.prepared_at) + timedelta(
            seconds=attempt.policy.start_seconds
        )
    if attempt.state is AttemptState.CANCEL_PENDING:
        limit = _cancel_limit(attempt)
        settled_from = _stop_settling_since(attempt)
        if settled_from is not None:
            return min(
                limit, settled_from + timedelta(seconds=attempt.policy.settle_seconds)
            )
        return limit
    if attempt.state in {AttemptState.START_CONFIRMED, AttemptState.COMPLETION_PENDING}:
        limit = (attempt.observed_start_at or attempt.prepared_at) + timedelta(
            seconds=attempt.policy.run_seconds
        )
        if attempt.terminal_observed_at is not None:
            return min(
                limit,
                attempt.terminal_observed_at
                + timedelta(seconds=attempt.policy.settle_seconds),
            )
        return limit
    return None


def evaluate_observation(
    attempt: ExecutionAttempt,
    unit: WorkUnit,
    assignment: DispatchAssignment,
    observation: RobotObservation,
    now: datetime,
) -> MonitorDecision:
    """Require cleaning activity and a normal end; errors never imply success."""
    if attempt.state not in {
        AttemptState.COMMAND_SENT,
        AttemptState.START_CONFIRMED,
        AttemptState.COMPLETION_PENDING,
        AttemptState.CANCEL_PENDING,
    }:
        return MonitorDecision(MonitorAction.WAIT, "attempt_not_observing")
    observed_at = observation.observed_at or now
    if observed_at > now or observed_at < (
        attempt.last_observation_at
        or attempt.command_boundary_at
        or attempt.prepared_at
    ):
        deadline = next_deadline(attempt)
        if deadline is not None and now >= deadline:
            return MonitorDecision(MonitorAction.ATTENTION, "observation_timeout")
        return MonitorDecision(MonitorAction.WAIT, "observation_out_of_order")
    if observation.source_robot_id != attempt.source_robot_id:
        return MonitorDecision(MonitorAction.ATTENTION, "observation_source_mismatch")
    if observation.error_code is not None:
        return MonitorDecision(MonitorAction.ATTENTION, "robot_reported_error")
    if observation.state in {
        RobotAvailabilityState.UNKNOWN,
        RobotAvailabilityState.UNAVAILABLE,
    }:
        return MonitorDecision(MonitorAction.ATTENTION, "robot_connection_lost")
    if attempt.state is AttemptState.CANCEL_PENDING:
        return _evaluate_cancel(attempt, observation, observed_at, now)
    if attempt.state is AttemptState.COMMAND_SENT:
        if now >= (next_deadline(attempt) or now):
            return MonitorDecision(MonitorAction.ATTENTION, "start_timeout")
        if observation.cleaning_active is True:
            if (
                observation.observed_operation is not None
                and observation.observed_operation != unit.operation
            ):
                return MonitorDecision(
                    MonitorAction.ATTENTION, "observed_mode_mismatch"
                )
            return MonitorDecision(MonitorAction.START, "cleaning_start_observed")
        return MonitorDecision(MonitorAction.WAIT, "awaiting_cleaning_start")
    if now >= (attempt.observed_start_at or attempt.prepared_at) + timedelta(
        seconds=attempt.policy.run_seconds
    ):
        return MonitorDecision(MonitorAction.ATTENTION, "run_timeout")
    if (
        observation.observed_operation is not None
        and observation.observed_operation != unit.operation
    ):
        return MonitorDecision(MonitorAction.ATTENTION, "observed_mode_mismatch")
    if not observation.normal_end or observation.cleaning_active is not False:
        return MonitorDecision(
            MonitorAction.RESUME
            if attempt.terminal_observed_at is not None
            else MonitorAction.WAIT,
            "cleaning_not_finished",
        )
    if (
        observation.completion_confirmed
        and observation.completed_targets
        and set(observation.completed_targets) != set(assignment.adapter_targets)
    ):
        return MonitorDecision(MonitorAction.ATTENTION, "completion_scope_mismatch")
    if (
        observation.completion_confirmed
        and observation.observed_operation == unit.operation
        and set(observation.completed_targets) == set(assignment.adapter_targets)
    ):
        return MonitorDecision(
            MonitorAction.COMPLETE,
            "scope_mode_and_success_confirmed",
            CompletionQuality.CONFIRMED,
        )
    if attempt.terminal_observed_at is None:
        return MonitorDecision(MonitorAction.SETTLE, "awaiting_terminal_stability")
    if now < attempt.terminal_observed_at + timedelta(
        seconds=attempt.policy.settle_seconds
    ):
        return MonitorDecision(MonitorAction.WAIT, "awaiting_terminal_stability")
    return MonitorDecision(
        MonitorAction.COMPLETE,
        "observed_start_and_stable_normal_end",
        CompletionQuality.DERIVED,
    )


def _cancel_limit(attempt: ExecutionAttempt) -> datetime:
    """Return when a pending cancel needs attention, independent of settling."""
    return (attempt.cancel_requested_at or attempt.prepared_at) + timedelta(
        seconds=attempt.policy.cancel_seconds
        + (attempt.policy.return_seconds if attempt.return_to_dock else 0)
    )


def _stop_settling_since(attempt: ExecutionAttempt) -> datetime | None:
    """Return idle evidence that started at or after the sent stop."""
    stop = attempt.stop_sent_at
    terminal = attempt.terminal_observed_at
    return terminal if stop is not None and terminal and terminal >= stop else None


def _evaluate_cancel(
    attempt: ExecutionAttempt,
    observation: RobotObservation,
    observed_at: datetime,
    now: datetime,
) -> MonitorDecision:
    """Confirm cancel only from stable idle evidence sampled after the stop.

    A return to the dock is confirmed only once the robot rests at the dock;
    driving home, mop washing and emptying keep the cancel pending.
    """
    timed_out = now >= _cancel_limit(attempt)
    stop = attempt.stop_sent_at
    stopped = (
        observation.normal_end
        and observation.cleaning_active is False
        and (not attempt.return_to_dock or observation.at_dock is True)
    )
    settling = _stop_settling_since(attempt)
    if stop is not None and observed_at >= stop and stopped:
        if settling is None:
            return MonitorDecision(MonitorAction.SETTLE, "awaiting_stop_stability")
        if now >= settling + timedelta(seconds=attempt.policy.settle_seconds):
            return MonitorDecision(MonitorAction.CANCEL, "stop_observed")
    if timed_out:
        return MonitorDecision(MonitorAction.ATTENTION, "cancel_timeout")
    if stop is not None and observed_at >= stop and not stopped and settling:
        return MonitorDecision(MonitorAction.RESUME, "stop_not_observed")
    return MonitorDecision(
        MonitorAction.WAIT,
        "awaiting_stop_stability" if settling else "awaiting_stop",
    )
