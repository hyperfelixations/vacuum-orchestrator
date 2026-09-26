"""Tests for the global registry, pending queue, and execution ledger."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from custom_components.vacuum_orchestrator.domain.errors import (
    ConflictError,
    ValidationError,
)
from custom_components.vacuum_orchestrator.domain.execution import (
    ExecutionAttempt,
    RobotLease,
    RobotRun,
    RunCorrelation,
)
from custom_components.vacuum_orchestrator.domain.intents import (
    JobIntent,
    JobIntentPatch,
    TargetRef,
)
from custom_components.vacuum_orchestrator.domain.planning import (
    DispatchAssignment,
    ExecutionPlan,
    Planner,
    PreferenceResolution,
    WorkUnit,
)
from custom_components.vacuum_orchestrator.domain.queue import OrchestratorState
from custom_components.vacuum_orchestrator.domain.types import (
    AttemptState,
    CleaningMode,
    CorrelationConfidence,
    CorrelationState,
    JobState,
    MoveDirection,
    QueueMode,
    WorkUnitState,
)

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
INTENT = JobIntent((TargetRef("kitchen"),), CleaningMode.VACUUM)


def _queue() -> OrchestratorState:
    state = OrchestratorState.empty("installation")
    for job_id in ("a", "b", "c"):
        state = state.add_job(job_id, INTENT, NOW)
    return state


def _prepared(
    intent: JobIntent = INTENT,
) -> tuple[OrchestratorState, str]:
    state, plan, unit, assignment, attempt, lease = _dispatch_parts(intent)
    return (
        state.prepare_dispatch("a", plan, unit, assignment, attempt, lease, NOW),
        unit.work_unit_id,
    )


def _dispatch_parts(
    intent: JobIntent = INTENT,
) -> tuple[
    OrchestratorState,
    ExecutionPlan,
    WorkUnit,
    DispatchAssignment,
    ExecutionAttempt,
    RobotLease,
]:
    state = OrchestratorState.empty("installation").add_job("a", intent, NOW)
    plan = Planner().create_plan("a", intent)
    unit = plan.work_units[0]
    assignment = DispatchAssignment(
        unit.work_unit_id,
        "robot",
        "source",
        "fake",
        ("16",),
        "caps",
        PreferenceResolution((), ()),
    )
    attempt = ExecutionAttempt(
        "attempt",
        "a",
        unit.work_unit_id,
        "robot",
        "source",
        1,
        AttemptState.PREPARED,
        NOW,
    )
    lease = RobotLease("source", "robot", "attempt", unit.work_unit_id, 1)
    return state, plan, unit, assignment, attempt, lease


def test_global_queue_has_no_robot_or_fleet_job_ownership() -> None:
    state = _queue()

    assert state.installation_id == "installation"
    assert not hasattr(state.jobs["a"], "robot_id")
    assert state.queue == ("a", "b", "c")


@pytest.mark.parametrize(
    ("direction", "expected"),
    [
        (MoveDirection.UP, ("b", "a", "c")),
        (MoveDirection.DOWN, ("a", "c", "b")),
        (MoveDirection.TOP, ("b", "a", "c")),
        (MoveDirection.BOTTOM, ("a", "c", "b")),
    ],
)
def test_move_directions_are_user_facing(
    direction: MoveDirection, expected: tuple[str, ...]
) -> None:
    assert _queue().move_job("b", direction).queue == expected


def test_edge_move_is_noop_and_revision_is_internal() -> None:
    state = _queue()
    assert state.move_job("a", MoveDirection.UP) is state
    updated = state.update_job("a", JobIntentPatch(note="changed"), NOW)
    assert updated.jobs["a"].revision == 2
    assert updated.jobs["a"].intent.note == "changed"


def test_dispatch_removes_only_started_job_from_pending_queue() -> None:
    state, unit_id = _prepared()

    assert state.jobs["a"].state is JobState.DISPATCHING
    assert state.queue == ()
    assert state.work_unit_states[unit_id].value == "active"
    assert state.robot_leases["source"].attempt_id == "attempt"


def test_strong_completion_finishes_single_unit_job() -> None:
    prepared, unit_id = _prepared()
    running = prepared.mark_command_sent("attempt", NOW)
    running = running.mark_dispatch_accepted("attempt", NOW)
    running = running.mark_start_confirmed("attempt", NOW)
    run = RobotRun("run", "source", NOW, NOW, NOW, NOW, cleaning_activity_seen=True)
    correlation = RunCorrelation(
        "attempt",
        "run",
        CorrelationState.MATCHED,
        CorrelationConfidence.STRONG,
        ("activity_and_new_history_pair",),
        False,
    )
    completed = running.complete_attempt("attempt", run, correlation, NOW)

    assert completed.jobs["a"].state is JobState.COMPLETED
    assert completed.jobs["a"].completed_work_unit_ids == (unit_id,)
    assert completed.robot_leases == {}


def test_two_phase_completion_exposes_next_dependency_satisfied_unit() -> None:
    intent = JobIntent((TargetRef("kitchen"),), CleaningMode.VACUUM_THEN_MOP)
    prepared, first_id = _prepared(intent)
    state = prepared.mark_command_sent("attempt", NOW)
    state = state.mark_start_confirmed("attempt", NOW)
    run = RobotRun("run", "source", NOW, NOW, NOW, NOW, cleaning_activity_seen=True)
    correlation = RunCorrelation(
        "attempt",
        "run",
        CorrelationState.MATCHED,
        CorrelationConfidence.STRONG,
        (),
        False,
    )
    state = state.complete_attempt("attempt", run, correlation, NOW)

    assert state.jobs["a"].state is JobState.DISPATCHING
    assert state.next_pending_unit(state.jobs["a"]).depends_on == (first_id,)


def test_uncertain_run_blocks_only_affected_robot() -> None:
    prepared, _unit_id = _prepared()
    state = prepared.mark_command_sent("attempt", NOW)
    run = RobotRun("run", "source")
    correlation = RunCorrelation(
        "attempt",
        "run",
        CorrelationState.UNCERTAIN,
        CorrelationConfidence.WEAK,
        ("incomplete",),
        False,
    )
    blocked = state.complete_attempt("attempt", run, correlation, NOW)

    assert blocked.mode is QueueMode.IDLE
    assert blocked.blocked_robots == {"source": "physical_run_ownership_uncertain"}
    assert blocked.jobs["a"].state is JobState.NEEDS_ATTENTION


def test_cancel_retry_delete_lifecycles_are_distinct() -> None:
    queued = OrchestratorState.empty("installation").add_job("a", INTENT, NOW)
    cancelled, generation = queued.request_cancel("a", NOW)
    retried = cancelled.retry_job("a", "retry", NOW)
    deleted = retried.delete_job("a")

    assert generation is None
    assert retried.jobs["a"].state is JobState.CANCELLED
    assert retried.jobs["retry"].retries_job_id == "a"
    assert "a" not in deleted.jobs


def test_active_cancel_fences_before_confirmation() -> None:
    prepared, _unit_id = _prepared()
    requested, generation = prepared.request_cancel("a", NOW)
    confirmed = requested.confirm_cancel("a", NOW)

    assert generation == 2
    assert requested.jobs["a"].state is JobState.CANCELING
    assert requested.attempts["attempt"].state is AttemptState.CANCEL_PENDING
    assert confirmed.jobs["a"].state is JobState.CANCELLED


def test_active_jobs_cannot_be_edited_moved_or_deleted() -> None:
    state, _unit_id = _prepared()

    with pytest.raises(ConflictError, match="job_not_editable"):
        state.update_job("a", JobIntentPatch(note="x"), NOW)
    with pytest.raises(ConflictError, match="job_not_movable"):
        state.move_job("a", MoveDirection.UP)
    with pytest.raises(ConflictError, match="job_not_deletable"):
        state.delete_job("a")


def test_invariants_reject_duplicate_dedupe_key_and_bad_queue() -> None:
    intent = replace(INTENT, dedupe_key="auto|kitchen")
    state = OrchestratorState.empty("installation").add_job("a", intent, NOW)
    with pytest.raises(ConflictError, match="dedupe_key_already_queued"):
        state.add_job("b", intent, NOW)
    with pytest.raises(Exception, match="duplicate_queue_job"):
        replace(state, queue=("a", "a"))


def test_global_queue_structural_invariants_fail_closed() -> None:
    queued = OrchestratorState.empty("installation").add_job("a", INTENT, NOW)
    cancelled, _ = queued.request_cancel("a", NOW)

    with pytest.raises(ValidationError, match="empty_installation_id"):
        OrchestratorState.empty(" ")
    with pytest.raises(ValidationError, match="queue_references_unknown_job"):
        replace(queued, queue=("missing",))
    with pytest.raises(ValidationError, match="queue_contains_non_queued_job"):
        replace(cancelled, queue=("a",))
    with pytest.raises(ValidationError, match="queued_job_missing_from_queue"):
        replace(queued, queue=())


def test_add_update_and_mode_conflicts_are_internalized() -> None:
    state = OrchestratorState.empty("installation").add_job("a", INTENT, NOW)
    with pytest.raises(ConflictError, match="job_already_exists"):
        state.add_job("a", INTENT, NOW)
    assert state.update_job("a", JobIntentPatch(note=None), NOW) is state
    assert state.set_queue_mode(QueueMode.IDLE) is state
    assert state.set_queue_mode(QueueMode.RUNNING).mode is QueueMode.RUNNING

    other = replace(INTENT, dedupe_key="same")
    state = state.add_job("b", other, NOW)
    with pytest.raises(ConflictError, match="dedupe_key_already_queued"):
        state.update_job("a", JobIntentPatch(dedupe_key="same"), NOW)


def test_attempt_transition_preconditions_are_enforced() -> None:
    prepared, _ = _prepared()
    with pytest.raises(ConflictError, match="attempt_not_prepared"):
        prepared.mark_command_sent("attempt", NOW).mark_command_sent("attempt", NOW)
    with pytest.raises(ConflictError, match="attempt_command_not_sent"):
        prepared.mark_dispatch_accepted("attempt", NOW)
    with pytest.raises(ConflictError, match="attempt_not_awaiting_start"):
        prepared.mark_start_confirmed("attempt", NOW)
    with pytest.raises(ValidationError, match="unknown_attempt"):
        prepared.mark_command_sent("missing", NOW)

    sent = prepared.mark_command_sent("attempt", NOW)
    canceled, _ = sent.request_cancel("a", NOW)
    assert canceled.mark_dispatch_accepted("attempt", NOW) is canceled


def test_repeated_dispatch_acceptance_preserves_observed_start() -> None:
    prepared, _ = _prepared()
    started = prepared.mark_command_sent("attempt", NOW).mark_start_confirmed(
        "attempt", NOW
    )
    accepted = started.mark_dispatch_accepted("attempt", NOW)

    assert accepted.jobs["a"].state is JobState.RUNNING
    assert accepted.attempts["attempt"] == started.attempts["attempt"]
    assert accepted.mark_dispatch_accepted("attempt", NOW) is accepted


def test_completion_replay_is_idempotent_and_collision_is_rejected() -> None:
    prepared, _ = _prepared()
    sent = prepared.mark_command_sent("attempt", NOW)
    run = RobotRun("run", "source", NOW, NOW, NOW, NOW, True)
    correlation = RunCorrelation(
        "attempt",
        "run",
        CorrelationState.MATCHED,
        CorrelationConfidence.STRONG,
        ("history",),
        False,
    )
    completed = sent.complete_attempt("attempt", run, correlation, NOW)

    assert completed.complete_attempt("attempt", run, correlation, NOW) is completed
    conflicting_run = replace(run, observed_end=None)
    with pytest.raises(ConflictError, match="run_correlation_identity_conflict"):
        completed.complete_attempt("attempt", conflicting_run, correlation, NOW)
    with pytest.raises(ValidationError, match="correlation_identity_mismatch"):
        sent.complete_attempt(
            "attempt", run, replace(correlation, attempt_id="other"), NOW
        )


def test_failure_and_retry_preconditions_preserve_history() -> None:
    queued = OrchestratorState.empty("installation").add_job("a", INTENT, NOW)
    with pytest.raises(ConflictError, match="job_not_retryable"):
        queued.retry_job("a", "retry", NOW)

    prepared, unit_id = _prepared()
    with pytest.raises(ConflictError, match="attempt_ownership_conflict"):
        prepared.fail_job("a", "wrong", "device_error", NOW)
    failed = prepared.fail_job("a", "attempt", "device_error", NOW)

    assert failed.jobs["a"].state is JobState.FAILED
    assert failed.attempts["attempt"].failure_code == "device_error"
    assert failed.work_unit_states[unit_id] is WorkUnitState.FAILED
    assert failed.robot_leases == {}
    with pytest.raises(ConflictError, match="job_already_exists"):
        failed.retry_job("a", "a", NOW)


def test_cancel_preconditions_reject_incoherent_ownership() -> None:
    queued = OrchestratorState.empty("installation").add_job("a", INTENT, NOW)
    cancelled, _ = queued.request_cancel("a", NOW)
    with pytest.raises(ConflictError, match="job_not_cancellable"):
        cancelled.request_cancel("a", NOW)
    with pytest.raises(ConflictError, match="cancel_not_requested"):
        queued.confirm_cancel("a", NOW)

    prepared, _ = _prepared()
    without_lease = replace(prepared, robot_leases={})
    with pytest.raises(ConflictError, match="attempt_lease_missing"):
        without_lease.request_cancel("a", NOW)


def test_restart_attention_reserves_targets_and_skips_existing_fence() -> None:
    prepared, _ = _prepared()
    blocked = prepared.require_attention_for_active_leases(NOW)

    assert blocked.needs_attention
    assert blocked.active_target_sets() == (frozenset({"kitchen"}),)
    assert blocked.require_attention_for_active_leases(NOW) is blocked


def test_pending_unit_and_lookup_failures_are_explicit() -> None:
    queued = OrchestratorState.empty("installation").add_job("a", INTENT, NOW)
    with pytest.raises(ConflictError, match="job_plan_missing"):
        queued.next_pending_unit(queued.jobs["a"])
    with pytest.raises(ValidationError, match="unknown_job"):
        queued.delete_job("missing")

    prepared, unit_id = _prepared()
    exhausted = replace(
        prepared,
        jobs={"a": replace(prepared.jobs["a"], completed_work_unit_ids=(unit_id,))},
        work_unit_states={unit_id: WorkUnitState.COMPLETED},
    )
    with pytest.raises(ConflictError, match="job_has_no_pending_work_unit"):
        exhausted.next_pending_unit(exhausted.jobs["a"])
