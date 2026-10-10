"""Translate pure monitoring decisions into one atomic ledger transition."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime

from ..domain.completion import CompletionQuality, evidenced_operation
from ..domain.correlation import correlate_run
from ..domain.dispatching import RobotObservation
from ..domain.execution import RobotRun
from ..domain.monitoring import (
    MonitorAction,
    MonitorDecision,
    evaluate_observation,
    record_observation,
)
from ..domain.planning import DispatchAssignment, WorkUnit
from ..domain.queue import OrchestratorState
from ..domain.types import AttemptState, OperationKind


@dataclass(frozen=True, slots=True)
class ObservationResult:
    """A candidate state and the monitor decision it came from.

    `targets` compares reported completed targets with the assignment:
    `none`, `complete`, `partial` or `mismatch`. See dev doc "Strukturierte
    HA-Protokollierung".
    """

    state: OrchestratorState
    decision: MonitorDecision | None = None
    operation: OperationKind | None = None
    targets: str | None = None


def apply_observation(
    state: OrchestratorState,
    observation: RobotObservation,
    now: datetime,
    id_factory: Callable[[], str],
) -> ObservationResult:
    """Keep success receipts and execution state inside the same critical write."""
    lease = state.robot_leases.get(observation.source_robot_id)
    if lease is None:
        return ObservationResult(state)
    attempt = state.attempts[lease.attempt_id]
    job = state.jobs[attempt.job_id]
    plan = state.plans[job.plan_id or ""]
    unit = next(
        item for item in plan.work_units if item.work_unit_id == attempt.work_unit_id
    )
    assignment = state.assignments[attempt.attempt_id]
    decision = evaluate_observation(attempt, unit, assignment, observation, now)
    reported, assigned = (
        set(observation.completed_targets),
        set(assignment.adapter_targets),
    )
    recorded = record_observation(
        attempt,
        replace(observation, observed_at=observation.observed_at or now),
        decision,
    )
    tracked = (
        state
        if recorded == attempt
        else replace(state, attempts={**state.attempts, attempt.attempt_id: recorded})
    )
    candidate = _transition(tracked, observation, now, id_factory, decision)
    if candidate is tracked and tracked is not state:
        candidate = replace(tracked, commit_id=state.commit_id + 1)
    return ObservationResult(
        candidate,
        decision,
        unit.operation,
        targets="none"
        if not reported
        else "complete"
        if reported == assigned
        else "partial"
        if reported < assigned
        else "mismatch",
    )


def _evidenced_rooms(
    unit: WorkUnit, assignment: DispatchAssignment, completed: set[str]
) -> tuple[str, ...]:
    """Return the unit's rooms whose targets the robot confirmed as cleaned."""
    if not completed or completed >= set(assignment.adapter_targets):
        return unit.canonical_targets
    return tuple(
        room_id
        for room_id in unit.canonical_targets
        if (targets := assignment.room_targets.get(room_id))
        and set(targets) <= completed
    )


def _transition(
    state: OrchestratorState,
    observation: RobotObservation,
    now: datetime,
    id_factory: Callable[[], str],
    decision: MonitorDecision,
) -> OrchestratorState:
    lease = state.robot_leases[observation.source_robot_id]
    attempt = state.attempts[lease.attempt_id]
    job = state.jobs[attempt.job_id]
    plan = state.plans[job.plan_id or ""]
    unit = next(
        item for item in plan.work_units if item.work_unit_id == attempt.work_unit_id
    )
    assignment = state.assignments[attempt.attempt_id]
    observed_at = observation.observed_at or now
    if decision.action is MonitorAction.WAIT:
        return state
    if decision.action is MonitorAction.ATTENTION:
        return state.require_robot_attention(
            attempt.attempt_id, decision.reason, None, None, now
        )
    if decision.action is MonitorAction.START:
        return state.mark_start_confirmed(attempt.attempt_id, observed_at)
    if decision.action is MonitorAction.FAIL:
        return state.fail_job(
            job.job_id,
            attempt.attempt_id,
            decision.reason,
            now,
            never_started=attempt.observed_start_at is None,
        )
    if decision.action is MonitorAction.FAULT_WAIT:
        return replace(
            state,
            commit_id=state.commit_id + 1,
            attempts={
                **state.attempts,
                attempt.attempt_id: replace(
                    attempt, fault_since=observed_at, last_observation_at=observed_at
                ),
            },
        )
    if decision.action is MonitorAction.CANCEL:
        return state.confirm_cancel(job.job_id, observed_at)
    if decision.action in {MonitorAction.SETTLE, MonitorAction.RESUME}:
        settling = decision.action is MonitorAction.SETTLE
        updated = replace(
            attempt,
            state=attempt.state
            if attempt.state is AttemptState.CANCEL_PENDING
            else AttemptState.COMPLETION_PENDING
            if settling
            else AttemptState.START_CONFIRMED,
            terminal_observed_at=observed_at if settling else None,
            last_observation_at=observed_at,
            fault_since=None,
        )
        return replace(
            state,
            commit_id=state.commit_id + 1,
            attempts={**state.attempts, attempt.attempt_id: updated},
        )
    history_start = observation.history_start
    history_end = observation.history_end
    if history_start == attempt.prior_history_start:
        history_start = None
    if history_end == attempt.prior_history_end:
        history_end = None
    # Deviation rule; see dev doc "Abweichungen".
    confirmed = observation.completion_confirmed and decision.quality is not None
    modes = attempt.observed_operations
    if confirmed and observation.completed_operation is not None:
        modes = (*modes, observation.completed_operation)
    completed = set(observation.completed_targets) if confirmed else set()
    # An end in a gap; see dev doc "Unterbrechungen".
    unobserved = (
        decision.quality is CompletionQuality.DERIVED
        and attempt.gap_since is not None
        and (
            attempt.terminal_observed_at is None
            or attempt.terminal_observed_at >= attempt.gap_since
        )
    )
    notes = tuple(
        code
        for code, changed in (
            ("mode_changed", any(mode != unit.operation for mode in modes)),
            (
                "scope_changed",
                bool(completed) and completed != set(assignment.adapter_targets),
            ),
            ("end_not_observed", unobserved),
        )
        if changed
    )
    run = RobotRun(
        id_factory(),
        attempt.source_robot_id,
        observed_start=attempt.observed_start_at,
        observed_end=observed_at,
        history_start=history_start,
        history_end=history_end,
        cleaning_activity_seen=True,
        completion_quality=decision.quality,
        operation=evidenced_operation(unit.operation, modes),
        canonical_targets=_evidenced_rooms(unit, assignment, completed),
    )
    correlation = correlate_run(attempt, run)
    correlation = replace(correlation, reason_codes=(*correlation.reason_codes, *notes))
    return state.complete_attempt(attempt.attempt_id, run, correlation, now)
