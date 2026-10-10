"""Translate pure monitoring decisions into one atomic ledger transition."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime

from ..domain.completion import CleaningReceipt, CleaningSource, CompletionQuality
from ..domain.correlation import correlate_run
from ..domain.dispatching import RobotObservation
from ..domain.execution import RobotRun
from ..domain.monitoring import MonitorAction, MonitorDecision, evaluate_observation
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
    return ObservationResult(
        _transition(state, observation, now, id_factory, decision),
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
        candidate = state.require_robot_attention(
            attempt.attempt_id, decision.reason, None, None, now
        )
        if (
            decision.reason == "completion_scope_mismatch"
            and observation.completed_operation == unit.operation
            and set(observation.completed_targets) < set(assignment.adapter_targets)
        ):
            completed_rooms = tuple(
                room_id
                for room_id, targets in assignment.room_targets.items()
                if set(targets) <= set(observation.completed_targets)
            )
            if completed_rooms:
                candidate = replace(
                    candidate,
                    room_registry=candidate.room_registry.record(
                        CleaningReceipt(
                            f"attempt:{attempt.attempt_id}:partial",
                            CleaningSource.VOI,
                            attempt.attempt_id,
                            completed_rooms,
                            unit.operation,
                            observed_at,
                            CompletionQuality.CONFIRMED,
                            ("confirmed_partial_scope",),
                        )
                    ),
                )
        return candidate
    if decision.action is MonitorAction.START:
        return state.mark_start_confirmed(attempt.attempt_id, observed_at)
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
    run = RobotRun(
        id_factory(),
        attempt.source_robot_id,
        observed_start=attempt.observed_start_at,
        observed_end=observed_at,
        history_start=history_start,
        history_end=history_end,
        cleaning_activity_seen=True,
        completion_quality=decision.quality,
        operation=unit.operation,
        canonical_targets=unit.canonical_targets,
    )
    correlation = correlate_run(attempt, run)
    return state.complete_attempt(attempt.attempt_id, run, correlation, now)
