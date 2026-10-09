"""A Roborock run that returns home completes.

The observation sequence mirrors a field run with synthetic identities.
"""

from dataclasses import replace
from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.vacuum_orchestrator.application.orchestrator import (
    VacuumOrchestrator,
)
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
from tests.adapters.test_roborock import setup_robot, simulate_settings
from tests.application.test_orchestrator import NOW, IdFactory, RecordingBackend


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


async def test_a_run_that_returns_home_with_another_mode_setting_completes(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
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

    async def observe(status: str, in_cleaning: str, seconds: float = 1) -> None:
        freezer.tick(timedelta(seconds=seconds))
        hass.states.async_set("sensor.status", status)
        hass.states.async_set("binary_sensor.in_cleaning", in_cleaning)
        await core.async_process_robot_observation("robot")

    def attempt():
        return next(iter(core.state.attempts.values()))

    await observe("segment_cleaning", "on")
    assert attempt().state is AttemptState.START_CONFIRMED
    mode = hass.states.get("select.cleaning_mode")
    assert mode is not None and mode.state == "vacuum"
    hass.states.async_set("select.cleaning_mode", "vac_and_mop", mode.attributes)

    await observe("returning_home", "off", seconds=870)
    assert core.state.jobs[job_id].state is JobState.RUNNING
    await observe("charging", "off", seconds=80)
    await observe("emptying_the_bin", "off", seconds=3)
    await observe("charging", "off", seconds=24)
    assert core.state.jobs[job_id].state is JobState.RUNNING
    await observe("charging", "off", seconds=30)

    assert core.state.jobs[job_id].state is JobState.COMPLETED
    assert attempt().completion_quality is CompletionQuality.DERIVED
    stamp = core.state.room_registry.rooms["kitchen"].last_cleaning
    assert OperationKind.VACUUM in stamp
    await observe("charging", "off", seconds=30)
    assert core.state.room_registry.rooms["kitchen"].last_cleaning == stamp

    reloaded = await _core(backend, adapter)
    assert reloaded.state.jobs[job_id].state is JobState.COMPLETED
