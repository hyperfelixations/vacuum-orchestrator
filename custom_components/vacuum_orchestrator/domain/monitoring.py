"""Pure execution monitoring; evidence quality is independent of cleaning mode."""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum

from .completion import CompletionQuality
from .dispatching import RobotObservation
from .execution import ExecutionAttempt
from .faults import interrupting
from .planning import DispatchAssignment, WorkUnit
from .types import AttemptState, RobotAvailabilityState, RobotPhase


class MonitorAction(StrEnum):
    """Transitions that the single writer may persist from an observation."""

    WAIT = "wait"
    START = "start"
    SETTLE = "settle"
    RESUME = "resume"
    COMPLETE = "complete"
    CANCEL = "cancel"
    ATTENTION = "attention"
    FAULT_WAIT = "fault_wait"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class MonitorDecision:
    """One explainable transition, with quality only for successful completion."""

    action: MonitorAction
    reason: str
    quality: CompletionQuality | None = None


def next_deadline(attempt: ExecutionAttempt) -> datetime | None:
    """Return a persisted attempt's next deadline without polling devices."""
    if attempt.state is AttemptState.COMMAND_SENT:
        return attempt.start_deadline
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
        if attempt.fault_since is not None:
            limit = min(limit, _fault_limit(attempt))
        if attempt.connection_deadline is not None:
            limit = min(limit, attempt.connection_deadline)
        if attempt.fault_since is not None:
            return limit
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
    stopped = observation.normal_end and observation.cleaning_active is False
    cleaning = observation.cleaning_active is True
    # Fault rule; see dev doc "Gerätefehler".
    faulted = interrupting(
        observation.faults,
        unit.operation,
        finished=stopped and attempt.fault_since is None,
        cleaning=cleaning,
    )
    lost = observation.state in {
        RobotAvailabilityState.UNKNOWN,
        RobotAvailabilityState.UNAVAILABLE,
    }
    if attempt.state is AttemptState.CANCEL_PENDING:
        # A cancel needs a stop, not a finished run; any resting robot counts.
        if interrupting(
            observation.faults, unit.operation, finished=stopped, cleaning=cleaning
        ):
            return MonitorDecision(MonitorAction.ATTENTION, "robot_reported_error")
        if lost:
            return MonitorDecision(MonitorAction.ATTENTION, "robot_connection_lost")
        return _evaluate_cancel(attempt, observation, observed_at, now)
    if attempt.state is AttemptState.COMMAND_SENT:
        preparing = _preparing(observation)
        if preparing and attempt.preparing_since is None:
            attempt = replace(attempt, preparing_since=observed_at)
        if now >= attempt.start_deadline:
            # A robot proven to rest never started; see dev doc "Unterbrechungen".
            if stopped and not lost:
                return MonitorDecision(MonitorAction.FAIL, "start_not_observed")
            return MonitorDecision(MonitorAction.ATTENTION, "start_timeout")
        if faulted:
            return MonitorDecision(MonitorAction.WAIT, "robot_fault")
        if lost:
            return MonitorDecision(MonitorAction.WAIT, "robot_connection_lost")
        if observation.cleaning_active is True:
            return MonitorDecision(MonitorAction.START, "cleaning_start_observed")
        return MonitorDecision(
            MonitorAction.WAIT,
            "preparing_cleaning" if preparing else "awaiting_cleaning_start",
        )
    if now >= (attempt.observed_start_at or attempt.prepared_at) + timedelta(
        seconds=attempt.policy.run_seconds
    ):
        return MonitorDecision(MonitorAction.ATTENTION, "run_timeout")
    if faulted:
        if attempt.fault_since is None:
            return MonitorDecision(MonitorAction.FAULT_WAIT, "robot_fault")
        if now >= _fault_limit(attempt):
            return _fault_timeout(stopped)
        return MonitorDecision(MonitorAction.WAIT, "robot_fault")
    if lost:
        # Connection window; see dev doc "Unterbrechungen".
        deadline = attempt.connection_deadline
        if deadline is not None and now >= deadline:
            return MonitorDecision(MonitorAction.ATTENTION, "robot_connection_lost")
        return MonitorDecision(MonitorAction.WAIT, "robot_connection_lost")
    if attempt.fault_since is not None and observation.cleaning_active is not True:
        if _confirmed(observation):
            return _confirmed_completion(observation, unit, assignment)
        if now >= _fault_limit(attempt):
            return _fault_timeout(stopped)
        return MonitorDecision(MonitorAction.WAIT, "awaiting_cleaning_after_fault")
    if attempt.fault_since is not None:
        return MonitorDecision(MonitorAction.RESUME, "fault_cleared")
    if attempt.gap_since is not None and observation.cleaning_active is True:
        return MonitorDecision(MonitorAction.RESUME, "cleaning_observed_after_gap")
    if not observation.normal_end or observation.cleaning_active is not False:
        return MonitorDecision(
            MonitorAction.RESUME
            if attempt.terminal_observed_at is not None
            else MonitorAction.WAIT,
            "cleaning_not_finished",
        )
    if _confirmed(observation):
        return _confirmed_completion(observation, unit, assignment)
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


# Decisions about observations that cannot describe this attempt's run.
_UNRELATED = frozenset(
    {
        "attempt_not_observing",
        "observation_out_of_order",
        "observation_source_mismatch",
        "observation_timeout",
    }
)
_WATCHED = frozenset(
    {
        AttemptState.COMMAND_SENT,
        AttemptState.START_CONFIRMED,
        AttemptState.COMPLETION_PENDING,
    }
)


def record_observation(
    attempt: ExecutionAttempt,
    observation: RobotObservation,
    decision: MonitorDecision,
) -> ExecutionAttempt:
    """Keep what a run's observations showed between transitions.

    Modes set while cleaning (dev doc "Abweichungen") and observation gaps
    (dev doc "Unterbrechungen").
    """
    if attempt.state not in _WATCHED or decision.reason in _UNRELATED:
        return attempt
    observed_at = observation.observed_at
    lost = decision.reason == "robot_connection_lost"
    cleaning = observation.cleaning_active is True
    operation = observation.observed_operation
    started = attempt.state is not AttemptState.COMMAND_SENT
    return replace(
        attempt,
        observed_operations=(*attempt.observed_operations, operation)
        if cleaning
        and operation is not None
        and operation not in attempt.observed_operations
        else attempt.observed_operations,
        lost_since=(attempt.lost_since or observed_at) if lost else None,
        preparing_since=attempt.preparing_since
        or (observed_at if not started and _preparing(observation) else None),
        gap_since=None
        if cleaning
        else (attempt.gap_since or observed_at)
        if lost and started
        else attempt.gap_since,
    )


def _preparing(observation: RobotObservation) -> bool:
    """Return whether the robot works at its dock, as before mopping."""
    return (
        observation.phase is RobotPhase.STATION
        and observation.state is RobotAvailabilityState.BUSY
    )


def _confirmed(observation: RobotObservation) -> bool:
    """Return whether the robot confirms a finished run with mode and scope."""
    return (
        observation.completion_confirmed
        and observation.completed_operation is not None
        and bool(observation.completed_targets)
    )


def _confirmed_completion(
    observation: RobotObservation, unit: WorkUnit, assignment: DispatchAssignment
) -> MonitorDecision:
    """Complete on confirmation; see dev doc "Abweichungen" for differences."""
    exact = observation.completed_operation == unit.operation and set(
        observation.completed_targets
    ) == set(assignment.adapter_targets)
    return MonitorDecision(
        MonitorAction.COMPLETE,
        "scope_mode_and_success_confirmed"
        if exact
        else "success_confirmed_with_changes",
        CompletionQuality.CONFIRMED,
    )


def _fault_limit(attempt: ExecutionAttempt) -> datetime:
    deadline = attempt.fault_deadline
    assert deadline is not None
    return deadline


def _fault_timeout(stopped: bool) -> MonitorDecision:
    """Fail only a robot proven to rest; otherwise recovery keeps ownership."""
    return MonitorDecision(
        MonitorAction.FAIL if stopped else MonitorAction.ATTENTION,
        "robot_fault_timeout",
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
