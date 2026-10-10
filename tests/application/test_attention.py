"""Attention lists what a person must act on now; see dev doc "Aufmerksamkeit"."""

from dataclasses import replace
from datetime import timedelta

from custom_components.vacuum_orchestrator.domain.attention import (
    Attention,
    AttentionKind,
)
from custom_components.vacuum_orchestrator.domain.faults import (
    Fault,
    FaultScope,
    FaultSource,
)
from custom_components.vacuum_orchestrator.domain.types import (
    CleaningMode,
    OperationKind,
)
from tests.application.test_orchestrator import (
    NOW,
    RecordingAdapter,
    RecordingBackend,
    _intent,
)
from tests.application.test_waiting import setup_waiting

TANK = Fault("water_empty", FaultSource.DOCK, FaultScope.STATION_MOP, "sensor.dock")
AUDIO = Fault("audio_error", FaultSource.ROBOT, FaultScope.NOTICE, "sensor.error")


async def test_a_device_fault_needs_attention_at_once_without_a_job() -> None:
    adapter = RecordingAdapter(RecordingBackend(), "robot")
    core, _now = await setup_waiting(adapter)
    assert core.attention() == ()

    adapter.observation = replace(adapter.observation, faults=(TANK, AUDIO))
    await core.async_process_robot_observation("robot")
    later = NOW + timedelta(minutes=5)
    adapter.observation = replace(adapter.observation, observed_at=later)
    await core.async_process_robot_observation("robot")

    assert core.attention() == (
        Attention(
            AttentionKind.DEVICE_FAULT,
            "robot",
            None,
            ("water_empty",),
            frozenset({OperationKind.MOP, OperationKind.VACUUM_AND_MOP}),
            NOW,
        ),
    )
    adapter.observation = replace(adapter.observation, faults=(AUDIO,))
    await core.async_process_robot_observation("robot")
    assert core.attention() == ()


async def test_faults_of_operations_a_robot_never_runs_need_no_attention() -> None:
    adapter = RecordingAdapter(RecordingBackend(), "robot")
    adapter._profile = replace(
        adapter.profile, allowed_operations=frozenset({OperationKind.VACUUM})
    )
    core, _now = await setup_waiting(adapter)

    adapter.observation = replace(adapter.observation, faults=(TANK,))
    await core.async_process_robot_observation("robot")

    assert core.attention() == ()


async def test_recovery_of_a_robot_needs_attention() -> None:
    adapter = RecordingAdapter(RecordingBackend(), "robot")
    core, _now = await setup_waiting(adapter)
    state = core.state
    core._state = replace(
        state, blocked_robots={adapter.profile.source_robot_id: "stop_unconfirmed"}
    )

    assert core.attention() == (
        Attention(AttentionKind.ROBOT_RECOVERY, "robot", None, ("stop_unconfirmed",)),
    )


async def test_a_removed_robot_forgets_when_its_fault_began() -> None:
    adapter = RecordingAdapter(RecordingBackend(), "robot")
    core, _now = await setup_waiting(adapter)
    adapter.observation = replace(adapter.observation, faults=(TANK,))
    await core.async_process_robot_observation("robot")

    await core.async_replace_adapters({})
    await core.async_replace_adapters({"robot": adapter})
    later = NOW + timedelta(minutes=5)
    adapter.observation = replace(adapter.observation, observed_at=later)
    await core.async_process_robot_observation("robot")

    assert [entry.since for entry in core.attention()] == [later]


DETACHED = Fault(
    "mop_detached",
    FaultSource.ROBOT,
    FaultScope.MOP,
    "binary_sensor.mop",
    needs_action=False,
)


async def test_a_removed_mop_needs_attention_only_once_a_mop_job_waits_for_it() -> None:
    adapter = RecordingAdapter(RecordingBackend(), "robot")
    core, _now = await setup_waiting(adapter)
    adapter.observation = replace(adapter.observation, faults=(DETACHED,))
    await core.async_process_robot_observation("robot")
    assert core.attention() == ()

    vacuuming = await core.async_create_job(_intent())
    assert core.attention() == ()
    await core.async_delete_job(vacuuming)

    await core.async_create_job(_intent(mode=CleaningMode.MOP))
    (entry,) = core.attention()
    assert (entry.kind, entry.codes, entry.since) == (
        AttentionKind.DEVICE_FAULT,
        ("mop_detached",),
        NOW,
    )


async def test_a_removed_mop_needs_no_attention_while_another_robot_mops() -> None:
    first = RecordingAdapter(RecordingBackend(), "robot")
    second = RecordingAdapter(RecordingBackend(), "other")
    core, _now = await setup_waiting(first, second)
    first.observation = replace(first.observation, faults=(DETACHED,))
    await core.async_process_robot_observation("robot")

    await core.async_create_job(_intent(mode=CleaningMode.MOP))

    assert core.attention() == ()


async def test_a_mop_removed_during_a_mop_run_needs_attention() -> None:
    adapter = RecordingAdapter(RecordingBackend(), "robot")
    core, _now = await setup_waiting(adapter)
    job = await core.async_create_job(_intent(mode=CleaningMode.MOP))
    await core.async_start_job(job)
    adapter.observation = replace(adapter.observation, faults=(DETACHED,))
    await core.async_process_robot_observation("robot")

    (entry,) = core.attention()
    assert (entry.codes, entry.job_id) == (("mop_detached",), None)
