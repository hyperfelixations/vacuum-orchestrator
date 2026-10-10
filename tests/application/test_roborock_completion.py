"""A Roborock run that returns home completes.

The observation sequence mirrors a field run with synthetic identities.
"""

from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import cast

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.vacuum_orchestrator.adapters.public_values import (
    ADAPTER_VALUES,
)
from custom_components.vacuum_orchestrator.adapters.roborock import RoborockAdapter
from custom_components.vacuum_orchestrator.api.presentation import present_job_view
from custom_components.vacuum_orchestrator.application.orchestrator import (
    VacuumOrchestrator,
)
from custom_components.vacuum_orchestrator.diagnostics import build_diagnostics
from custom_components.vacuum_orchestrator.domain.completion import CompletionQuality
from custom_components.vacuum_orchestrator.domain.intents import JobIntent, TargetRef
from custom_components.vacuum_orchestrator.domain.queue import OrchestratorState
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
    OperationKind,
)
from custom_components.vacuum_orchestrator.infrastructure.critical_repository import (
    CriticalOrchestratorRepository,
)
from custom_components.vacuum_orchestrator.infrastructure.telemetry import (
    LoggingSink,
)
from custom_components.vacuum_orchestrator.runtime import VacuumOrchestratorRuntime
from tests.adapters.test_roborock import setup_robot, simulate_settings
from tests.application.test_orchestrator import NOW, IdFactory, RecordingBackend

DECISION = (
    "event",
    "phase",
    "reason",
    "cleaning_active",
    "normal_end",
    "completion_confirmed",
    "operation",
    "observed_operation",
    "previous_state",
    "monitor_action",
    "monitor_reason",
)


async def _core(backend: RecordingBackend, adapter: object) -> VacuumOrchestrator:
    core = VacuumOrchestrator(
        "installation",
        CriticalOrchestratorRepository(backend),
        {"robot": adapter},
        state_reader=dict.fromkeys,
        clock=dt_util.utcnow,
        id_factory=IdFactory(),
    )
    await core.async_initialize()
    return core


async def _started(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> tuple[VacuumOrchestrator, str, RecordingBackend, RoborockAdapter]:
    """Start a vacuum job in the kitchen with a real Roborock adapter."""
    freezer.move_to(NOW)
    adapter, _inventory, calls = setup_robot(hass)
    await adapter.async_refresh_maps()
    simulate_settings(hass)
    backend = RecordingBackend()
    kitchen = Room(
        "kitchen",
        "Kitchen",
        area_id="kitchen",
        release=RoomRelease("grant", ReleaseKind.PERMANENT, NOW),
    )
    await CriticalOrchestratorRepository(backend).async_commit(
        replace(
            OrchestratorState.empty("installation"),
            room_registry=RoomRegistry({"kitchen": kitchen}),
        ),
        expected_previous_commit_id=-1,
    )
    core = await _core(backend, adapter)
    await core.async_process_robot_observation("robot")
    job_id, _assignment = await core.async_create_and_start_job(
        JobIntent((TargetRef("kitchen"),), CleaningMode.VACUUM)
    )
    assert calls
    return core, job_id, backend, adapter


def _observer(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, core: VacuumOrchestrator
) -> Callable[..., Awaitable[None]]:
    async def observe(status: str, in_cleaning: str, seconds: float = 1) -> None:
        freezer.tick(timedelta(seconds=seconds))
        hass.states.async_set("sensor.status", status)
        hass.states.async_set("binary_sensor.in_cleaning", in_cleaning)
        await core.async_process_robot_observation("robot")

    return observe


def _set_mode(hass: HomeAssistant, option: str) -> None:
    mode = hass.states.get("select.cleaning_mode")
    assert mode is not None
    hass.states.async_set("select.cleaning_mode", option, mode.attributes)


async def test_a_run_that_returns_home_with_another_mode_setting_completes(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    core, job_id, backend, adapter = await _started(hass, freezer)
    observe = _observer(hass, freezer, core)

    def attempt():
        return next(iter(core.state.attempts.values()))

    await observe("segment_cleaning", "on")
    assert attempt().state is AttemptState.START_CONFIRMED
    assert hass.states.get("select.cleaning_mode").state == "vacuum"
    _set_mode(hass, "vac_and_mop")

    await observe("returning_home", "off", seconds=870)
    assert core.state.jobs[job_id].state is JobState.RUNNING
    returning = LoggingSink(ADAPTER_VALUES).sanitize(core.trace.snapshot()[-1])
    assert {key: returning[key] for key in DECISION} == {
        "event": "robot_observation",
        "phase": "returning",
        "reason": "returning_home",
        "cleaning_active": False,
        "normal_end": False,
        "completion_confirmed": False,
        "operation": "vacuum",
        "observed_operation": "vacuum_and_mop",
        "previous_state": "start_confirmed",
        "monitor_action": "wait",
        "monitor_reason": "cleaning_not_finished",
    }
    await observe("charging", "off", seconds=80)
    await observe("emptying_the_bin", "off", seconds=3)
    await observe("charging", "off", seconds=24)
    assert core.state.jobs[job_id].state is JobState.RUNNING
    await observe("charging", "off", seconds=30)

    assert core.state.jobs[job_id].state is JobState.COMPLETED
    assert attempt().completion_quality is CompletionQuality.DERIVED
    assert core.state.incidents == ()
    # A setting changed after cleaning is no deviation of the run.
    assert attempt().observed_operations == (OperationKind.VACUUM,)
    assert present_job_view(core, core.state.jobs[job_id])["completion"] == {
        "quality": "derived",
        "notes": [],
    }
    stamp = core.state.room_registry.rooms["kitchen"].last_cleaning
    assert OperationKind.VACUUM in stamp
    await observe("charging", "off", seconds=30)
    assert core.state.room_registry.rooms["kitchen"].last_cleaning == stamp

    reloaded = await _core(backend, adapter)
    assert reloaded.state.jobs[job_id].state is JobState.COMPLETED


async def _finish(observe: Callable[..., Awaitable[None]]) -> None:
    await observe("returning_home", "off", seconds=300)
    await observe("charging", "off", seconds=60)
    await observe("charging", "off", seconds=30)


async def test_a_mode_changed_while_cleaning_completes_with_what_both_covered(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    core, job_id, backend, adapter = await _started(hass, freezer)
    observe = _observer(hass, freezer, core)
    await observe("segment_cleaning", "on")
    _set_mode(hass, "vac_and_mop")
    await observe("segment_cleaning", "on", seconds=60)
    assert core.state.jobs[job_id].state is JobState.RUNNING
    await _finish(observe)

    assert core.state.jobs[job_id].state is JobState.COMPLETED
    (attempt,) = core.state.attempts.values()
    assert attempt.observed_operations == (
        OperationKind.VACUUM,
        OperationKind.VACUUM_AND_MOP,
    )
    (receipt,) = core.state.room_registry.receipts.values()
    assert (receipt.operation, receipt.room_ids) == (OperationKind.VACUUM, ("kitchen",))
    assert receipt.evidence == ("derived_completion", "mode_changed")
    assert present_job_view(core, core.state.jobs[job_id])["completion"] == {
        "quality": "derived",
        "notes": ["mode_changed"],
    }
    reloaded = await _core(backend, adapter)
    assert reloaded.state.attempts == core.state.attempts


async def test_a_run_switched_to_another_operation_completes_without_a_receipt(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    core, job_id, _backend, _adapter = await _started(hass, freezer)
    observe = _observer(hass, freezer, core)
    await observe("segment_cleaning", "on")
    _set_mode(hass, "mop")
    await observe("segment_cleaning", "on", seconds=60)
    await _finish(observe)

    assert core.state.jobs[job_id].state is JobState.COMPLETED
    assert core.state.room_registry.receipts == {}
    assert core.state.room_registry.rooms["kitchen"].last_cleaning == {}
    (run,) = core.state.robot_runs.values()
    assert (run.operation, run.canonical_targets) == (None, ("kitchen",))
    assert core.state.completion(job_id) is not None
    assert core.state.completion(job_id).notes == ("mode_changed",)
    assert not core.state.robot_leases
    (incident,) = core.state.incidents
    assert (incident.job_id, incident.observation.monitor_action) == (
        job_id,
        "complete",
    )


async def test_a_recovery_export_shows_its_cause_and_triggering_observation(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    core, job_id, backend, adapter = await _started(hass, freezer)
    observe = _observer(hass, freezer, core)
    await observe("segment_cleaning", "on")
    hass.states.async_set("sensor.error", "main_brush_jammed")
    await observe("segment_cleaning", "on", seconds=60)
    await observe("segment_cleaning", "on", seconds=900)
    assert core.state.jobs[job_id].state is JobState.NEEDS_ATTENTION
    hass.states.async_set("sensor.error", "none")
    await observe("charging", "off", seconds=600)
    await core.async_resolve_recovery("robot")
    # The incident is part of the stored state and survives a restart.
    await core.async_shutdown()
    core = await _core(backend, adapter)

    export = build_diagnostics(
        cast(VacuumOrchestratorRuntime, SimpleNamespace(orchestrator=core))
    )

    (attempt,) = export["attempts"]
    assert (
        attempt["state"],
        attempt["failure_code"],
        attempt["recovery_resolution"],
    ) == ("failed", "robot_fault_timeout", "verified_stopped")
    incident = attempt["incident"]
    assert (
        incident["phase"],
        incident["cleaning_active"],
        incident["observed_operation"],
        incident["operation"],
        incident["monitor_action"],
        incident["monitor_reason"],
        incident["faults"],
    ) == (
        "cleaning",
        True,
        "vacuum",
        "vacuum",
        "attention",
        "robot_fault_timeout",
        "main_brush_jammed:vacuum:robot",
    )
    assert incident["job_id"] == attempt["job_id"]
    window = export["trace_window"]
    assert window["capacity"] == 512 and window["first_sequence"] == 1
    assert window["last_sequence"] == window["recorded"]
    assert export["diagnostic_version"] == 3
    assert "kitchen" not in str(export["attempts"])
