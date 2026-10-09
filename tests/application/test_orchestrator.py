"""Transactional scheduling, persistence, and observation tests."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.vacuum_orchestrator.application.orchestrator import (
    VacuumOrchestrator,
)
from custom_components.vacuum_orchestrator.domain.capabilities import (
    AreaAddressing,
    CancelSemantics,
    CompletionEvidence,
    PassCapability,
    RobotCapabilities,
    RobotProfile,
    StartEvidence,
)
from custom_components.vacuum_orchestrator.domain.dispatching import RobotObservation
from custom_components.vacuum_orchestrator.domain.errors import (
    ConflictError,
    PlanningError,
    StorageIntegrityError,
)
from custom_components.vacuum_orchestrator.domain.execution import RobotRun
from custom_components.vacuum_orchestrator.domain.intents import (
    JobIntent,
    JobIntentPatch,
    TargetRef,
)
from custom_components.vacuum_orchestrator.domain.maps import RobotMaps
from custom_components.vacuum_orchestrator.domain.planning import (
    DispatchAssignment,
    WorkUnit,
)
from custom_components.vacuum_orchestrator.domain.queue import OrchestratorState
from custom_components.vacuum_orchestrator.domain.reach import ReachStatus, RoomReach
from custom_components.vacuum_orchestrator.domain.releases import (
    ReleaseKind,
    RoomRelease,
)
from custom_components.vacuum_orchestrator.domain.room_registry import RoomRegistry
from custom_components.vacuum_orchestrator.domain.rooms import Room
from custom_components.vacuum_orchestrator.domain.types import (
    AttemptState,
    CleaningMode,
    JobState,
    MoveDirection,
    OperationKind,
    PassScope,
    QueueMode,
    RobotAvailabilityState,
)
from custom_components.vacuum_orchestrator.infrastructure.codec import (
    decode_orchestrator_state,
)
from custom_components.vacuum_orchestrator.infrastructure.critical_repository import (
    CriticalOrchestratorRepository,
)
from custom_components.vacuum_orchestrator.infrastructure.integrity import (
    JsonObject,
    verify_snapshot,
)

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


class RecordingBackend:
    """Atomic in-memory backend with fault injection."""

    atomic_writes = True

    def __init__(self) -> None:
        self.data: JsonObject | None = None
        self.swallow_next_save = False

    async def async_load_raw(self) -> JsonObject | None:
        return deepcopy(self.data)

    async def async_save_raw(self, data: JsonObject) -> None:
        if self.swallow_next_save:
            self.swallow_next_save = False
            return
        self.data = deepcopy(data)


class RecordingAdapter:
    """Configurable adapter recording durable state at physical I/O."""

    def __init__(
        self,
        backend: RecordingBackend,
        robot_id: str,
        *,
        preference: int = 0,
        targets: tuple[str, ...] = ("kitchen", "hall"),
    ) -> None:
        capabilities = RobotCapabilities(
            revision=f"caps-{robot_id}",
            operations=frozenset(OperationKind),
            area_addressing=AreaAddressing.HOME_ASSISTANT_AREA,
            target_map={target: target for target in targets},
            map_context=None,
            passes=PassCapability(3, PassScope.TARGET_SET),
            vacuum_levels=frozenset(),
            water_levels=frozenset(),
            cancel=CancelSemantics.STOP,
            returns_to_dock=True,
            start_evidence=frozenset({StartEvidence.ACTIVITY_START_TRANSITION}),
            completion_evidence=frozenset(
                {
                    CompletionEvidence.ACTIVITY_TERMINAL_TRANSITION,
                    CompletionEvidence.CLEANING_HISTORY_TIMESTAMPS,
                }
            ),
        )
        self._profile = RobotProfile(
            robot_id,
            f"source-{robot_id}",
            "fake",
            capabilities,
            preference=preference,
        )
        self._backend = backend
        self.observation = RobotObservation(
            robot_id,
            f"source-{robot_id}",
            RobotAvailabilityState.AVAILABLE,
            80,
            observed_at=NOW,
            history_start=datetime(2026, 9, 7, 10, tzinfo=UTC),
            history_end=datetime(2026, 9, 7, 10, 10, tzinfo=UTC),
        )
        self.dispatches: list[tuple[WorkUnit, DispatchAssignment, AttemptState]] = []
        self.prepared: list[AttemptState] = []
        self.cancel_count = 0
        self.cancel_returns: list[bool] = []
        self.return_count = 0
        self.fail_dispatch = False
        self.fail_cancel = False
        self.confirm_completions = True

    @property
    def profile(self) -> RobotProfile:
        return self._profile

    def room_reach(self) -> tuple[RoomReach, ...]:
        return tuple(
            RoomReach(target, ReachStatus.REACHABLE, (target,))
            for target in self._profile.capabilities.target_map
        )

    def maps(self) -> RobotMaps:
        return RobotMaps()

    async def async_observe(self) -> RobotObservation:
        work = self.dispatches[-1][0] if self.dispatches else None
        assignment = self.dispatches[-1][1] if self.dispatches else None
        return replace(
            self.observation,
            cleaning_active=self.observation.state is RobotAvailabilityState.BUSY
            if self.observation.cleaning_active is None
            else self.observation.cleaning_active,
            normal_end=self.observation.state is RobotAvailabilityState.AVAILABLE,
            completion_confirmed=self.confirm_completions,
            observed_operation=work.operation if work else None,
            completed_targets=assignment.adapter_targets if assignment else (),
        )

    def _persisted_attempt_state(self) -> AttemptState:
        assert self._backend.data is not None
        state = decode_orchestrator_state(verify_snapshot(self._backend.data))
        return state.attempts[next(reversed(state.attempts))].state

    async def async_prepare(
        self, unit: WorkUnit, assignment: DispatchAssignment
    ) -> None:
        self.prepared.append(self._persisted_attempt_state())

    async def async_start(self, unit: WorkUnit, assignment: DispatchAssignment) -> None:
        if self.fail_dispatch:
            raise RuntimeError("dispatch uncertainty")
        self.dispatches.append((unit, assignment, self._persisted_attempt_state()))

    async def async_cancel(self, *, return_to_dock: bool = False) -> None:
        if self.fail_cancel:
            raise RuntimeError("cancel uncertainty")
        self.cancel_count += 1
        self.cancel_returns.append(return_to_dock)

    async def async_return_to_dock(self) -> None:
        self.return_count += 1


class IdFactory:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self) -> str:
        self.value += 1
        return f"id-{self.value}"


class DoubleFailureAdapter(RecordingAdapter):
    """Lose the recovery commit after an uncertain physical dispatch."""

    async def async_start(self, unit: WorkUnit, assignment: DispatchAssignment) -> None:
        self._backend.swallow_next_save = True
        raise RuntimeError("dispatch and storage uncertainty")


def _intent(
    area: str = "kitchen", mode: CleaningMode = CleaningMode.VACUUM
) -> JobIntent:
    return JobIntent((TargetRef(area),), mode)


async def _orchestrator(
    backend: RecordingBackend,
    *adapters: RecordingAdapter,
    states: dict[str, str | None] | None = None,
    seed_rooms: bool = True,
    start_delay: float = 0,
) -> VacuumOrchestrator:
    if backend.data is None:
        rooms = {
            name: Room(
                name,
                name.title(),
                area_id=name,
                release=RoomRelease(f"grant-{name}", ReleaseKind.PERMANENT, NOW),
            )
            for name in (("kitchen", "hall") if seed_rooms else ())
        }
        await CriticalOrchestratorRepository(backend).async_commit(
            replace(
                OrchestratorState.empty("installation"),
                room_registry=RoomRegistry(rooms),
                start_delay_seconds=start_delay,
            ),
            expected_previous_commit_id=-1,
        )
    orchestrator = VacuumOrchestrator(
        "installation",
        CriticalOrchestratorRepository(backend),
        {adapter.profile.robot_id: adapter for adapter in adapters},
        state_reader=lambda refs: {item: (states or {}).get(item) for item in refs},
        clock=lambda: max(
            (NOW, *(adapter.observation.observed_at or NOW for adapter in adapters))
        ),
        id_factory=IdFactory(),
    )
    await orchestrator.async_initialize()
    return orchestrator


async def test_start_observation_before_dispatch_returns_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = RecordingBackend()
    adapter = RecordingAdapter(backend, "robot")
    orchestrator = await _orchestrator(backend, adapter)
    job_id = await orchestrator.async_create_job(_intent())
    dispatch = adapter.async_start

    async def early_observation(unit: WorkUnit, assignment: DispatchAssignment) -> None:
        await dispatch(unit, assignment)
        adapter.observation = replace(
            adapter.observation, state=RobotAvailabilityState.BUSY
        )
        await orchestrator.async_process_robot_observation("robot")

    monkeypatch.setattr(adapter, "async_start", early_observation)
    await orchestrator.async_start_job(job_id)

    job = orchestrator.state.jobs[job_id]
    assert job.state is JobState.RUNNING
    assert (
        orchestrator.state.attempts[job.active_attempt_id].state
        is AttemptState.START_CONFIRMED
    )


async def test_start_job_auto_selects_best_robot_after_shared_availability_check() -> (
    None
):
    backend = RecordingBackend()
    low = RecordingAdapter(backend, "low")
    best = RecordingAdapter(backend, "best", preference=10)
    orchestrator = await _orchestrator(backend, low, best)
    job_id = await orchestrator.async_create_job(_intent())

    assignment = await orchestrator.async_start_job(job_id)

    assert assignment.robot_id == "best"
    assert best.dispatches[0][2] is AttemptState.COMMAND_SENT
    assert low.dispatches == []


async def test_start_job_honors_optional_robot_and_leaves_job_queued_if_busy() -> None:
    backend = RecordingBackend()
    first = RecordingAdapter(backend, "first")
    second = RecordingAdapter(backend, "second")
    second.observation = replace(second.observation, state=RobotAvailabilityState.BUSY)
    orchestrator = await _orchestrator(backend, first, second)
    job_id = await orchestrator.async_create_job(_intent())

    with pytest.raises(PlanningError, match="robot_busy"):
        await orchestrator.async_start_job(job_id, "second")

    assert orchestrator.state.queue == (job_id,)
    assignment = await orchestrator.async_start_job(job_id, "first")
    assert assignment.robot_id == "first"


async def test_readiness_is_separate_and_fail_closed() -> None:
    backend = RecordingBackend()
    adapter = RecordingAdapter(backend, "robot")
    orchestrator = await _orchestrator(
        backend, adapter, states={"binary_sensor.door": "off"}
    )
    job_id = await orchestrator.async_create_job(
        replace(_intent(), required_on=("binary_sensor.door",))
    )

    with pytest.raises(PlanningError, match="job_blocked"):
        await orchestrator.async_start_job(job_id)

    assert orchestrator.state.jobs[job_id].state is JobState.QUEUED


async def test_run_queue_dispatches_non_overlapping_jobs_to_distinct_robots() -> None:
    backend = RecordingBackend()
    first = RecordingAdapter(backend, "first")
    second = RecordingAdapter(backend, "second")
    orchestrator = await _orchestrator(backend, first, second)
    kitchen = await orchestrator.async_create_job(_intent("kitchen"))
    hall = await orchestrator.async_create_job(_intent("hall"))

    assignments = await orchestrator.async_run_queue()

    assert len(assignments) == 2
    assert {item.robot_id for item in assignments} == {"first", "second"}
    assert orchestrator.state.queue == ()
    assert orchestrator.state.jobs[kitchen].state is JobState.RUNNING
    assert orchestrator.state.jobs[hall].state is JobState.RUNNING


async def test_overlapping_job_remains_queued_until_area_is_free() -> None:
    backend = RecordingBackend()
    first = RecordingAdapter(backend, "first")
    second = RecordingAdapter(backend, "second")
    orchestrator = await _orchestrator(backend, first, second)
    first_job = await orchestrator.async_create_job(_intent())
    second_job = await orchestrator.async_create_job(_intent())

    assignments = await orchestrator.async_run_queue()

    assert len(assignments) == 1
    assert orchestrator.state.jobs[first_job].state is JobState.RUNNING
    assert orchestrator.state.queue == (second_job,)


async def test_observation_completes_job_and_automatically_dispatches_next() -> None:
    backend = RecordingBackend()
    adapter = RecordingAdapter(backend, "robot")
    orchestrator = await _orchestrator(backend, adapter)
    first = await orchestrator.async_create_job(_intent("kitchen"))
    second = await orchestrator.async_create_job(_intent("hall"))
    await orchestrator.async_run_queue()
    attempt_id = orchestrator.state.jobs[first].active_attempt_id
    assert attempt_id is not None

    adapter.observation = replace(
        adapter.observation,
        state=RobotAvailabilityState.BUSY,
        observed_at=datetime(2026, 9, 7, 12, 1, tzinfo=UTC),
    )
    await orchestrator.async_process_robot_observation("robot")
    adapter.observation = replace(
        adapter.observation,
        state=RobotAvailabilityState.AVAILABLE,
        observed_at=datetime(2026, 9, 7, 12, 10, tzinfo=UTC),
        history_start=datetime(2026, 9, 7, 12, 1, tzinfo=UTC),
        history_end=datetime(2026, 9, 7, 12, 9, tzinfo=UTC),
    )
    await orchestrator.async_process_robot_observation("robot")

    assert orchestrator.state.jobs[first].state is JobState.COMPLETED
    assert orchestrator.state.jobs[second].state is JobState.RUNNING
    assert len(adapter.dispatches) == 2


async def test_vacuum_then_mop_automatically_dispatches_dependent_unit() -> None:
    backend = RecordingBackend()
    adapter = RecordingAdapter(backend, "robot")
    orchestrator = await _orchestrator(backend, adapter)
    job_id = await orchestrator.async_create_job(
        _intent(mode=CleaningMode.VACUUM_THEN_MOP)
    )
    await orchestrator.async_start_job(job_id)
    adapter.observation = replace(
        adapter.observation,
        state=RobotAvailabilityState.BUSY,
        observed_at=datetime(2026, 9, 7, 12, 1, tzinfo=UTC),
    )
    await orchestrator.async_process_robot_observation("robot")
    adapter.observation = replace(
        adapter.observation,
        state=RobotAvailabilityState.AVAILABLE,
        observed_at=datetime(2026, 9, 7, 12, 10, tzinfo=UTC),
        history_start=datetime(2026, 9, 7, 12, 1, tzinfo=UTC),
        history_end=datetime(2026, 9, 7, 12, 9, tzinfo=UTC),
    )
    await orchestrator.async_process_robot_observation("robot")

    assert len(adapter.dispatches) == 2
    assert adapter.dispatches[1][0].operation is OperationKind.MOP


async def test_cancel_is_fenced_and_confirmed_by_shared_observation() -> None:
    backend = RecordingBackend()
    adapter = RecordingAdapter(backend, "robot")
    orchestrator = await _orchestrator(backend, adapter)
    job_id = await orchestrator.async_create_job(_intent())
    await orchestrator.async_start_job(job_id)

    await orchestrator.async_cancel_job(job_id)

    assert adapter.cancel_count == 1
    assert orchestrator.state.jobs[job_id].state is JobState.CANCELING
    for seconds in (1, 31):
        adapter.observation = replace(
            adapter.observation,
            state=RobotAvailabilityState.AVAILABLE,
            observed_at=NOW + timedelta(seconds=seconds),
        )
        await orchestrator.async_process_robot_observation("robot")
    assert orchestrator.state.jobs[job_id].state is JobState.CANCELLED


async def test_swallowed_critical_write_prevents_physical_io_and_blocks_mutations() -> (
    None
):
    backend = RecordingBackend()
    adapter = RecordingAdapter(backend, "robot")
    orchestrator = await _orchestrator(backend, adapter)
    job_id = await orchestrator.async_create_job(_intent())
    backend.swallow_next_save = True

    with pytest.raises(StorageIntegrityError, match="readback_mismatch"):
        await orchestrator.async_start_job(job_id)
    assert adapter.dispatches == []
    with pytest.raises(StorageIntegrityError, match="storage_state_uncertain"):
        await orchestrator.async_update_job(job_id, JobIntentPatch(note="x"))


async def test_physical_failure_isolates_only_affected_robot() -> None:
    backend = RecordingBackend()
    broken = RecordingAdapter(backend, "broken", preference=10)
    healthy = RecordingAdapter(backend, "healthy")
    broken.fail_dispatch = True
    orchestrator = await _orchestrator(backend, broken, healthy)
    failed_job = await orchestrator.async_create_job(_intent("kitchen"))

    with pytest.raises(RuntimeError, match="dispatch uncertainty"):
        await orchestrator.async_start_job(failed_job)

    assert orchestrator.state.jobs[failed_job].state is JobState.NEEDS_ATTENTION
    assert orchestrator.state.blocked_robots == {
        "source-broken": "physical_run_ownership_uncertain"
    }
    healthy_job = await orchestrator.async_create_job(_intent("hall"))
    assert (await orchestrator.async_start_job(healthy_job)).robot_id == "healthy"


async def test_restart_with_active_lease_fences_only_that_robot() -> None:
    backend = RecordingBackend()
    adapter = RecordingAdapter(backend, "robot")
    first = await _orchestrator(backend, adapter)
    job_id = await first.async_create_job(_intent())
    await first.async_start_job(job_id)
    restarted_adapter = RecordingAdapter(backend, "robot")
    restarted = await _orchestrator(backend, restarted_adapter)

    assert restarted.state.jobs[job_id].state is JobState.NEEDS_ATTENTION
    assert restarted.state.blocked_robots == {
        "source-robot": "physical_run_ownership_uncertain"
    }


async def test_simple_job_commands_need_no_public_revision() -> None:
    backend = RecordingBackend()
    adapter = RecordingAdapter(backend, "robot")
    orchestrator = await _orchestrator(backend, adapter)
    first = await orchestrator.async_create_job(_intent())
    second = await orchestrator.async_create_job(_intent("hall"))

    await orchestrator.async_update_job(first, JobIntentPatch(note="edited"))
    await orchestrator.async_move_job(second, MoveDirection.TOP)
    await orchestrator.async_cancel_job(first)
    retry = await orchestrator.async_retry_job(first)
    await orchestrator.async_delete_job(retry)
    await orchestrator.async_set_queue_mode(QueueMode.PAUSED)

    assert orchestrator.state.queue == (second,)
    assert orchestrator.state.mode is QueueMode.PAUSED


async def test_uninitialized_and_wrong_installation_state_are_rejected() -> None:
    backend = RecordingBackend()
    orchestrator = VacuumOrchestrator(
        "installation", CriticalOrchestratorRepository(backend), {}
    )
    with pytest.raises(ConflictError, match="orchestrator_not_initialized"):
        _ = orchestrator.state

    await CriticalOrchestratorRepository(backend).async_commit(
        OrchestratorState.empty("different"), expected_previous_commit_id=-1
    )
    with pytest.raises(StorageIntegrityError, match="installation_storage_ownership"):
        await orchestrator.async_initialize()


async def test_subscriber_receives_only_committed_changes() -> None:
    backend = RecordingBackend()
    orchestrator = await _orchestrator(backend)
    notifications: list[int] = []
    unsubscribe = orchestrator.subscribe(
        lambda: notifications.append(orchestrator.state.commit_id)
    )

    await orchestrator.async_set_queue_mode(QueueMode.PAUSED)
    await orchestrator.async_set_queue_mode(QueueMode.PAUSED)
    unsubscribe()
    await orchestrator.async_set_queue_mode(QueueMode.IDLE)

    assert notifications == [1]


async def test_readiness_and_observation_unknown_ids_are_explicit() -> None:
    backend = RecordingBackend()
    orchestrator = await _orchestrator(backend)

    with pytest.raises(ConflictError, match="unknown_job"):
        orchestrator.readiness_for_job("missing")
    with pytest.raises(ConflictError, match="unknown_robot"):
        await orchestrator.async_process_robot_observation("missing")
    with pytest.raises(ConflictError, match="unknown_attempt"):
        await orchestrator.async_record_robot_run("missing", RobotRun("run", "source"))


async def test_irrelevant_observations_do_not_advance_attempt() -> None:
    backend = RecordingBackend()
    adapter = RecordingAdapter(backend, "robot")
    orchestrator = await _orchestrator(backend, adapter)

    await orchestrator.async_process_robot_observation("robot")
    job_id = await orchestrator.async_create_job(_intent())
    await orchestrator.async_start_job(job_id)
    attempt_id = orchestrator.state.jobs[job_id].active_attempt_id
    assert attempt_id is not None

    await orchestrator.async_process_robot_observation("robot")
    assert orchestrator.state.attempts[attempt_id].state is AttemptState.COMMAND_SENT
    adapter.observation = replace(
        adapter.observation, state=RobotAvailabilityState.BUSY
    )
    await orchestrator.async_confirm_start(attempt_id)
    await orchestrator.async_process_robot_observation("robot")
    assert orchestrator.state.attempts[attempt_id].state is AttemptState.START_CONFIRMED


async def test_direct_observation_commands_complete_and_confirm_cancel() -> None:
    backend = RecordingBackend()
    adapter = RecordingAdapter(backend, "robot")
    orchestrator = await _orchestrator(backend, adapter)
    job_id = await orchestrator.async_create_job(_intent())
    await orchestrator.async_start_job(job_id)
    attempt_id = orchestrator.state.jobs[job_id].active_attempt_id
    assert attempt_id is not None
    await orchestrator.async_confirm_start(attempt_id, NOW)
    await orchestrator.async_record_robot_run(
        attempt_id,
        RobotRun(
            "physical-run",
            "source-robot",
            NOW,
            NOW,
            NOW,
            NOW,
            cleaning_activity_seen=True,
        ),
    )
    assert orchestrator.state.jobs[job_id].state is JobState.COMPLETED

    cancel_id = await orchestrator.async_create_job(_intent("hall"))
    await orchestrator.async_start_job(cancel_id)
    await orchestrator.async_cancel_job(cancel_id)
    await orchestrator.async_confirm_cancel(cancel_id)
    assert orchestrator.state.jobs[cancel_id].state is JobState.CANCELLED


async def test_cancel_failure_and_double_failure_are_fenced_fail_closed() -> None:
    backend = RecordingBackend()
    adapter = RecordingAdapter(backend, "robot")
    adapter.fail_cancel = True
    orchestrator = await _orchestrator(backend, adapter)
    job_id = await orchestrator.async_create_job(_intent())
    await orchestrator.async_start_job(job_id)

    with pytest.raises(RuntimeError, match="cancel uncertainty"):
        await orchestrator.async_cancel_job(job_id)
    assert orchestrator.state.jobs[job_id].state is JobState.NEEDS_ATTENTION

    second_backend = RecordingBackend()
    doubly_broken = DoubleFailureAdapter(second_backend, "broken")
    second = await _orchestrator(second_backend, doubly_broken)
    second_job = await second.async_create_job(_intent())
    with pytest.raises(RuntimeError, match="dispatch and storage uncertainty"):
        await second.async_start_job(second_job)
    with pytest.raises(StorageIntegrityError, match="storage_state_uncertain"):
        await second.async_set_queue_mode(QueueMode.PAUSED)


async def test_queue_runner_skips_currently_unstartable_jobs() -> None:
    backend = RecordingBackend()
    orchestrator = await _orchestrator(backend)
    job_id = await orchestrator.async_create_job(_intent())

    assert await orchestrator.async_run_queue() == ()
    assert orchestrator.state.queue == (job_id,)


def test_existing_plan_guard_rejects_incoherent_dispatching_job() -> None:
    state = OrchestratorState.empty("installation").add_job("job", _intent(), NOW)
    job = replace(state.jobs["job"], state=JobState.DISPATCHING)

    with pytest.raises(ConflictError, match="job_plan_missing"):
        VacuumOrchestrator._existing_plan(state, job)


async def test_robot_pool_change_during_observation_preserves_sample_identity(
    monkeypatch,
):
    backend = RecordingBackend()
    adapter = RecordingAdapter(backend, "robot")
    core = await _orchestrator(backend, adapter)

    async def observe_and_remove():
        await core.async_replace_adapters({})
        raise RuntimeError("device removed during sample")

    monkeypatch.setattr(adapter, "async_observe", observe_and_remove)
    samples = await core._observe_robots()
    assert samples["robot"].source_robot_id == "source-robot"
    assert samples["robot"].state is RobotAvailabilityState.UNKNOWN
    assert not core.adapters
