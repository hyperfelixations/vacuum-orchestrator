"""Room grants, prerequisites and reservations across complete job lifetimes."""

from dataclasses import replace
from datetime import timedelta

import pytest

from custom_components.vacuum_orchestrator.domain.completion import CompletionQuality
from custom_components.vacuum_orchestrator.domain.errors import (
    ConflictError,
    DispatchNotStartedError,
    PlanningError,
)
from custom_components.vacuum_orchestrator.domain.execution import RobotRun
from custom_components.vacuum_orchestrator.domain.intents import JobIntent, TargetRef
from custom_components.vacuum_orchestrator.domain.releases import ReleaseKind
from custom_components.vacuum_orchestrator.domain.requirements import StateRequirement
from custom_components.vacuum_orchestrator.domain.types import (
    AttemptState,
    CleaningMode,
    JobState,
    OperationKind,
    RobotAvailabilityState,
)
from custom_components.vacuum_orchestrator.ports.command_scope import (
    check_command_authorization,
)
from tests.application.test_orchestrator import (
    NOW,
    RecordingAdapter,
    RecordingBackend,
    _intent,
    _orchestrator,
)


def finished_run(attempt, name="run"):
    return RobotRun(
        name,
        attempt.source_robot_id,
        observed_start=NOW,
        observed_end=NOW + timedelta(minutes=10),
        history_start=NOW,
        history_end=NOW + timedelta(minutes=10),
        cleaning_activity_seen=True,
    )


async def test_room_grant_is_required_and_one_shot_belongs_to_started_job() -> None:
    backend = RecordingBackend()
    adapter = RecordingAdapter(backend, "robot")
    orchestrator = await _orchestrator(backend, adapter)
    await orchestrator.rooms.async_revoke("kitchen")
    job = await orchestrator.async_create_job(_intent())
    with pytest.raises(PlanningError, match="job_blocked"):
        await orchestrator.async_start_job(job)
    assert adapter.dispatches == []
    await orchestrator.rooms.async_grant("kitchen", ReleaseKind.ONCE)
    await orchestrator.async_start_job(job)
    room = orchestrator.rooms.registry.resolve("kitchen")
    assert room.release.reserved_job_id == job
    assert not room.release.consumed
    attempt = orchestrator.state.jobs[job].active_attempt_id
    await orchestrator.async_confirm_start(attempt)
    assert orchestrator.rooms.registry.resolve("kitchen").release.consumed
    await orchestrator.async_cancel_job(job)
    await orchestrator.async_confirm_cancel(job)
    retry = await orchestrator.async_retry_job(job)
    with pytest.raises(PlanningError, match="job_blocked"):
        await orchestrator.async_start_job(retry)
    assert job not in orchestrator.rooms.registry.admissions


async def test_expired_grant_preserves_both_phases_but_blocks_new_job() -> None:
    backend = RecordingBackend()
    adapter = RecordingAdapter(backend, "robot")
    orchestrator = await _orchestrator(backend, adapter)
    await orchestrator.rooms.async_grant("kitchen", ReleaseKind.TIMED, 60)
    job = await orchestrator.async_create_job(
        _intent(mode=CleaningMode.VACUUM_THEN_MOP)
    )
    await orchestrator.async_start_job(job)
    attempt_id = orchestrator.state.jobs[job].active_attempt_id
    await orchestrator.async_confirm_start(attempt_id)
    orchestrator._clock = lambda: NOW + timedelta(minutes=10)
    await orchestrator.async_record_robot_run(
        attempt_id, finished_run(orchestrator.state.attempts[attempt_id])
    )
    assert [item[0].operation for item in adapter.dispatches] == [
        OperationKind.VACUUM,
        OperationKind.MOP,
    ]
    assert orchestrator.rooms.registry.admissions[job][0].started
    second = await orchestrator.async_create_job(_intent())
    assert orchestrator.readiness_for_job(second).reason_codes == ("room_not_released",)


async def test_vacuum_receipt_counts_when_the_mop_phase_is_cancelled() -> None:
    backend = RecordingBackend()
    adapter = RecordingAdapter(backend, "robot")
    orchestrator = await _orchestrator(backend, adapter)
    job = await orchestrator.async_create_job(
        _intent(mode=CleaningMode.VACUUM_THEN_MOP)
    )
    await orchestrator.async_start_job(job)
    await orchestrator.async_confirm_start(
        orchestrator.state.jobs[job].active_attempt_id
    )
    adapter.observation = replace(
        adapter.observation, observed_at=NOW + timedelta(minutes=10)
    )
    await orchestrator.async_process_robot_observation("robot")
    mop_attempt = orchestrator.state.jobs[job].active_attempt_id
    assert adapter.dispatches[-1][0].operation is OperationKind.MOP
    await orchestrator.async_confirm_start(mop_attempt)

    await orchestrator.async_cancel_job(job)
    await orchestrator.async_confirm_cancel(job)

    room = orchestrator.rooms.registry.resolve("kitchen")
    assert orchestrator.state.jobs[job].state is JobState.CANCELLED
    assert room.last_confirmed[OperationKind.VACUUM].completed_at is not None
    assert OperationKind.MOP not in room.last_confirmed
    assert OperationKind.MOP not in room.last_cleaning


async def test_gap_between_phases_reserves_room_and_can_be_cancelled() -> None:
    backend = RecordingBackend()
    first = RecordingAdapter(backend, "first", preference=10)
    second = RecordingAdapter(backend, "second")
    orchestrator = await _orchestrator(backend, first, second)
    job = await orchestrator.async_create_job(
        _intent(mode=CleaningMode.VACUUM_THEN_MOP)
    )
    await orchestrator.async_start_job(job)
    attempt_id = orchestrator.state.jobs[job].active_attempt_id
    await orchestrator.async_confirm_start(attempt_id)
    for adapter in (first, second):
        adapter._profile = replace(
            adapter.profile, allowed_operations=frozenset({OperationKind.VACUUM})
        )
    await orchestrator.async_record_robot_run(
        attempt_id, finished_run(orchestrator.state.attempts[attempt_id])
    )
    assert orchestrator.state.jobs[job].active_attempt_id is None
    assert orchestrator.state.jobs[job].state is JobState.DISPATCHING
    other = await orchestrator.async_create_job(_intent())
    with pytest.raises(PlanningError, match="target_overlap_active"):
        await orchestrator.async_start_job(other)
    await orchestrator.async_cancel_job(job)
    assert orchestrator.state.jobs[job].state is JobState.CANCELLED
    assert job not in orchestrator.rooms.registry.admissions
    assert first.cancel_count == second.cancel_count == 0
    await orchestrator.async_start_job(other)


async def test_pre_start_failure_releases_one_shot_without_uncertainty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = RecordingBackend()
    adapter = RecordingAdapter(backend, "robot")
    orchestrator = await _orchestrator(backend, adapter)
    await orchestrator.rooms.async_grant("kitchen", ReleaseKind.ONCE)
    job = await orchestrator.async_create_job(_intent())

    async def no_start(*_args):
        raise DispatchNotStartedError("setting_confirmation_timeout")

    monkeypatch.setattr(adapter, "async_prepare", no_start)
    with pytest.raises(DispatchNotStartedError):
        await orchestrator.async_start_job(job)
    grant = orchestrator.rooms.registry.resolve("kitchen").release
    assert grant.reserved_job_id is None and not grant.consumed
    assert orchestrator.state.jobs[job].state is JobState.FAILED
    assert not orchestrator.state.robot_leases
    assert not orchestrator.state.blocked_robots


async def test_revocation_during_settings_prevents_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = RecordingBackend()
    adapter = RecordingAdapter(backend, "robot")
    orchestrator = await _orchestrator(backend, adapter)
    job = await orchestrator.async_create_job(_intent())

    async def settings_then_start(*_args):
        await orchestrator.rooms.async_revoke("kitchen")
        check_command_authorization()
        pytest.fail("Start must not be reached")

    monkeypatch.setattr(adapter, "async_prepare", settings_then_start)
    with pytest.raises(DispatchNotStartedError, match="readiness_changed"):
        await orchestrator.async_start_job(job)
    assert orchestrator.state.jobs[job].state is JobState.FAILED
    assert not orchestrator.rooms.registry.admissions


async def test_robot_specific_room_requirement_selects_other_robot() -> None:
    backend = RecordingBackend()
    first = RecordingAdapter(backend, "first", preference=100)
    second = RecordingAdapter(backend, "second")
    orchestrator = await _orchestrator(
        backend, first, second, states={"binary_sensor.door": "off"}
    )
    await orchestrator.rooms.async_update(
        "kitchen",
        lambda room: replace(
            room,
            requirements=(StateRequirement("binary_sensor.door", robot_id="first"),),
        ),
    )
    job = await orchestrator.async_create_job(_intent())
    assert orchestrator.readiness_for_job(job).state.value == "ready"
    assert (
        orchestrator.readiness_for_job(job, robot_id="first").state.value == "blocked"
    )
    assert (await orchestrator.async_start_job(job)).robot_id == "second"


async def test_bad_observer_and_listener_do_not_block_healthy_robot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = RecordingBackend()
    broken = RecordingAdapter(backend, "broken")
    healthy = RecordingAdapter(backend, "healthy")
    orchestrator = await _orchestrator(backend, broken, healthy)

    async def observe():
        raise RuntimeError("Unavailable adapter")

    def listener():
        raise RuntimeError("Presentation failed")

    monkeypatch.setattr(broken, "async_observe", observe)
    orchestrator.subscribe(listener)
    job = await orchestrator.async_create_job(_intent())
    assert (await orchestrator.async_start_job(job)).robot_id == "healthy"
    assert orchestrator.state.jobs[job].state is JobState.RUNNING


async def test_area_alias_is_canonicalized_and_duplicate_aliases_are_rejected() -> None:
    backend = RecordingBackend()
    orchestrator = await _orchestrator(backend, seed_rooms=False)
    room_id = await orchestrator.rooms.async_create("Kitchen", area_id="kitchen")
    job = await orchestrator.async_create_job(_intent())
    assert orchestrator.state.jobs[job].intent.areas == (TargetRef(room_id),)
    with pytest.raises(Exception, match="duplicate_area"):
        await orchestrator.async_create_job(
            JobIntent((TargetRef("kitchen"), TargetRef(room_id)), CleaningMode.VACUUM)
        )


async def test_derived_completion_projects_quality_and_preserves_last_confirmed() -> (
    None
):
    backend = RecordingBackend()
    adapter = RecordingAdapter(backend, "robot")
    orchestrator = await _orchestrator(backend, adapter)
    first = await orchestrator.async_create_job(_intent())
    await orchestrator.async_start_job(first)
    attempt_id = orchestrator.state.jobs[first].active_attempt_id
    await orchestrator.async_confirm_start(attempt_id)
    adapter.observation = replace(
        adapter.observation, observed_at=NOW + timedelta(minutes=10)
    )
    await orchestrator.async_process_robot_observation("robot")
    confirmed = orchestrator.rooms.registry.resolve("kitchen").last_confirmed[
        OperationKind.VACUUM
    ]
    adapter.confirm_completions = False
    second = await orchestrator.async_create_job(_intent())
    await orchestrator.async_start_job(second)
    attempt_id = orchestrator.state.jobs[second].active_attempt_id
    await orchestrator.async_confirm_start(attempt_id)
    adapter.observation = replace(
        adapter.observation, observed_at=NOW + timedelta(minutes=20)
    )
    await orchestrator.async_process_robot_observation("robot")
    assert (
        orchestrator.state.attempts[attempt_id].state is AttemptState.COMPLETION_PENDING
    )
    adapter.observation = replace(
        adapter.observation, observed_at=NOW + timedelta(minutes=20, seconds=30)
    )
    await orchestrator.async_process_robot_observation("robot")
    assert orchestrator.state.jobs[second].state is JobState.COMPLETED
    room = orchestrator.rooms.registry.resolve("kitchen")
    assert room.last_cleaning[OperationKind.VACUUM].quality is CompletionQuality.DERIVED
    assert room.last_confirmed[OperationKind.VACUUM] == confirmed
    assert (
        room.last_cleaning[OperationKind.VACUUM].completed_at > confirmed.completed_at
    )
    await orchestrator.async_delete_job(second)
    assert (
        orchestrator.rooms.registry.resolve("kitchen").last_cleaning
        == room.last_cleaning
    )


async def test_disconnect_retains_lease_and_never_writes_a_success_receipt() -> None:
    backend = RecordingBackend()
    adapter = RecordingAdapter(backend, "robot")
    orchestrator = await _orchestrator(backend, adapter)
    job = await orchestrator.async_create_job(_intent())
    await orchestrator.async_start_job(job)
    attempt_id = orchestrator.state.jobs[job].active_attempt_id
    await orchestrator.async_confirm_start(attempt_id)
    adapter.observation = replace(
        adapter.observation, state=RobotAvailabilityState.UNKNOWN
    )
    await orchestrator.async_process_robot_observation("robot")
    assert orchestrator.state.jobs[job].state is JobState.NEEDS_ATTENTION
    assert orchestrator.state.blocked_robots == {
        "source-robot": "robot_connection_lost"
    }
    assert orchestrator.state.robot_leases
    assert not orchestrator.rooms.registry.receipts


async def test_recovery_requires_stopped_robot_and_preserves_consumed_grant() -> None:
    backend = RecordingBackend()
    adapter = RecordingAdapter(backend, "robot")
    orchestrator = await _orchestrator(backend, adapter)
    await orchestrator.rooms.async_grant("kitchen", ReleaseKind.ONCE)
    job = await orchestrator.async_create_job(_intent())
    adapter.fail_dispatch = True
    with pytest.raises(RuntimeError):
        await orchestrator.async_start_job(job)
    adapter.observation = replace(
        adapter.observation, state=RobotAvailabilityState.BUSY
    )
    with pytest.raises(Exception, match="robot_stopped_confirmation_required"):
        await orchestrator.async_resolve_recovery("robot")
    assert orchestrator.state.robot_leases
    adapter.observation = replace(
        adapter.observation, state=RobotAvailabilityState.AVAILABLE
    )
    await orchestrator.async_resolve_recovery("robot")
    assert orchestrator.state.jobs[job].state is JobState.FAILED
    assert not orchestrator.state.robot_leases
    assert not orchestrator.state.blocked_robots
    assert orchestrator.rooms.registry.resolve("kitchen").release.consumed
    assert not orchestrator.rooms.registry.receipts


@pytest.mark.parametrize(
    "late_error", [RuntimeError("late"), DispatchNotStartedError("late")]
)
async def test_late_dispatch_error_preserves_completed_job(monkeypatch, late_error):
    backend = RecordingBackend()
    adapter = RecordingAdapter(backend, "robot")
    core = await _orchestrator(backend, adapter)
    job = await core.async_create_job(_intent())

    async def finish_then_error(*_args):
        attempt = core.state.jobs[job].active_attempt_id
        await core.async_confirm_start(attempt)
        await core.async_record_robot_run(
            attempt, finished_run(core.state.attempts[attempt])
        )
        raise late_error

    monkeypatch.setattr(adapter, "async_start", finish_then_error)
    with pytest.raises(type(late_error)):
        await core.async_start_job(job)
    assert core.state.jobs[job].state is JobState.COMPLETED
    assert not core.state.blocked_robots
    assert not core.state.robot_leases
    await core.async_create_job(_intent())


async def test_prestart_error_conflicting_with_observed_start_requires_recovery(
    monkeypatch,
):
    backend = RecordingBackend()
    adapter = RecordingAdapter(backend, "robot")
    core = await _orchestrator(backend, adapter)
    job = await core.async_create_job(_intent())

    async def contradictory(*_args):
        await core.async_confirm_start(core.state.jobs[job].active_attempt_id)
        raise DispatchNotStartedError("contradictory")

    monkeypatch.setattr(adapter, "async_start", contradictory)
    with pytest.raises(DispatchNotStartedError):
        await core.async_start_job(job)
    assert core.state.jobs[job].state is JobState.NEEDS_ATTENTION
    assert core.state.blocked_robots
    assert not core.rooms.registry.receipts


@pytest.mark.parametrize(
    "targets,expected_rooms",
    [(("a",), ()), (("a", "b"), ("kitchen",)), (("a", "foreign"), ())],
)
async def test_partial_completion_books_only_fully_proven_room_scope(
    monkeypatch, targets, expected_rooms
):
    backend = RecordingBackend()
    adapter = RecordingAdapter(backend, "robot")
    adapter._profile = replace(
        adapter.profile,
        capabilities=replace(
            adapter.profile.capabilities,
            target_map={"kitchen": ("a", "b"), "hall": "c"},
        ),
    )
    core = await _orchestrator(backend, adapter)
    job = await core.async_create_job(
        JobIntent((TargetRef("kitchen"), TargetRef("hall")), CleaningMode.VACUUM)
    )
    await core.async_start_job(job)
    await core.async_confirm_start(core.state.jobs[job].active_attempt_id)
    observed = replace(
        await adapter.async_observe(),
        observed_at=NOW + timedelta(minutes=10),
        completed_targets=targets,
    )
    adapter.observation = observed

    async def observe():
        return observed

    monkeypatch.setattr(adapter, "async_observe", observe)
    await core.async_process_robot_observation("robot")
    assert core.state.jobs[job].state is JobState.NEEDS_ATTENTION
    assert (
        tuple(
            room_id
            for room_id, room in core.rooms.registry.rooms.items()
            if room.last_cleaning
        )
        == expected_rooms
    )
    if expected_rooms:
        assert (
            core.rooms.registry.resolve("kitchen")
            .last_confirmed[OperationKind.VACUUM]
            .quality
            is CompletionQuality.CONFIRMED
        )


async def test_late_proof_upgrades_same_receipt_without_counting_cleaning_twice():
    backend = RecordingBackend()
    adapter = RecordingAdapter(backend, "robot")
    core = await _orchestrator(backend, adapter)
    adapter.confirm_completions = False
    job = await core.async_create_job(_intent())
    await core.async_start_job(job)
    attempt_id = core.state.jobs[job].active_attempt_id
    await core.async_confirm_start(attempt_id)
    for seconds in (600, 631):
        adapter.observation = replace(
            adapter.observation, observed_at=NOW + timedelta(seconds=seconds)
        )
        await core.async_process_robot_observation("robot")
    correlation = core.state.correlations[attempt_id]
    run = core.state.robot_runs[correlation.robot_run_id]
    stamp = core.rooms.registry.resolve("kitchen").last_cleaning[OperationKind.VACUUM]
    await core.async_record_robot_run(
        attempt_id, replace(run, completion_quality=CompletionQuality.CONFIRMED)
    )
    room = core.rooms.registry.resolve("kitchen")
    assert room.last_confirmed[OperationKind.VACUUM].completed_at == stamp.completed_at
    assert room.last_confirmed[OperationKind.VACUUM].receipt_id == stamp.receipt_id
    assert len(core.rooms.registry.receipts) == 1


async def test_removed_robot_and_legacy_unscoped_recovery_require_explicit_stop():
    backend = RecordingBackend()
    adapter = RecordingAdapter(backend, "robot")
    core = await _orchestrator(backend, adapter)
    job = await core.async_create_job(_intent())
    adapter.fail_dispatch = True
    with pytest.raises(RuntimeError):
        await core.async_start_job(job)
    reloaded = await _orchestrator(backend)
    with pytest.raises(Exception, match="robot_stopped_confirmation_required"):
        await reloaded.async_resolve_recovery("robot")
    await reloaded.async_resolve_recovery("robot", confirm_stopped=True)
    assert reloaded.state.jobs[job].failure_code == "operator_assumed_stopped"
    assert not reloaded.state.robot_leases and not reloaded.rooms.registry.receipts
    await reloaded._mutate(
        lambda state: replace(
            state,
            commit_id=state.commit_id + 1,
            blocked_robots={"legacy:unscoped": "legacy_recovery_required"},
        )
    )
    await reloaded.async_resolve_recovery("legacy:unscoped", confirm_stopped=True)
    assert not reloaded.state.needs_attention


async def test_readiness_is_reported_before_each_phase_but_never_while_running() -> (
    None
):
    backend = RecordingBackend()
    adapter = RecordingAdapter(backend, "robot")
    states = {"binary_sensor.mop_ready": "off"}
    orchestrator = await _orchestrator(backend, adapter, states=states)
    await orchestrator.rooms.async_update(
        "kitchen",
        lambda room: replace(
            room,
            requirements=(
                StateRequirement(
                    "binary_sensor.mop_ready", operation=OperationKind.MOP
                ),
            ),
        ),
    )
    job = await orchestrator.async_create_job(
        _intent(mode=CleaningMode.VACUUM_THEN_MOP)
    )
    assert orchestrator.readiness_before_start(job) is not None
    await orchestrator.async_start_job(job)
    assert orchestrator.readiness_before_start(job) is None
    attempt_id = orchestrator.state.jobs[job].active_attempt_id
    await orchestrator.async_confirm_start(attempt_id)
    await orchestrator.async_record_robot_run(
        attempt_id, finished_run(orchestrator.state.attempts[attempt_id])
    )

    assert orchestrator.state.jobs[job].state is JobState.DISPATCHING
    assert orchestrator.state.jobs[job].active_attempt_id is None
    report = orchestrator.readiness_before_start(job)
    assert report is not None
    assert report.state.value == "blocked"
    assert [item.entity_id for item in report.requirements] == [
        "binary_sensor.mop_ready"
    ]
    assert report.requirements[0].operation is OperationKind.MOP
    states["binary_sensor.mop_ready"] = "on"
    assert orchestrator.readiness_before_start(job).state.value == "ready"
    with pytest.raises(ConflictError, match="unknown_job"):
        orchestrator.readiness_before_start("missing")
