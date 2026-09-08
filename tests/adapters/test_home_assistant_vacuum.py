"""Contract tests for the public Home Assistant vacuum adapter."""

from datetime import UTC, datetime

import pytest
from homeassistant.components.vacuum.const import VacuumEntityFeature
from homeassistant.core import HomeAssistant, ServiceCall

from custom_components.vacuum_orchestrator.adapters.home_assistant_vacuum import (
    HomeAssistantVacuumAdapter,
)
from custom_components.vacuum_orchestrator.domain.errors import ConflictError
from custom_components.vacuum_orchestrator.domain.intents import CleaningPreferences
from custom_components.vacuum_orchestrator.domain.planning import (
    DispatchAssignment,
    PreferenceResolution,
    WorkUnit,
)
from custom_components.vacuum_orchestrator.domain.types import (
    OperationKind,
    PassScope,
    RobotAvailabilityState,
    SettingsPolicy,
)


def _adapter(hass: HomeAssistant) -> HomeAssistantVacuumAdapter:
    return HomeAssistantVacuumAdapter(
        hass,
        robot_id="robot",
        source_robot_id="source",
        entity_id="vacuum.test",
        adapter_name="roborock",
        target_areas=("kitchen",),
        last_clean_start_entity_id="sensor.start",
        last_clean_end_entity_id="sensor.end",
    )


def _unit() -> WorkUnit:
    return WorkUnit(
        "unit",
        OperationKind.VACUUM_AND_MOP,
        ("kitchen",),
        None,
        1,
        PassScope.TARGET_SET,
        CleaningPreferences(),
        SettingsPolicy.BEST_EFFORT,
        None,
        (),
    )


async def test_dispatch_uses_public_clean_area_with_late_assignment(
    hass: HomeAssistant,
) -> None:
    calls: list[ServiceCall] = []

    async def handle(call: ServiceCall) -> None:
        calls.append(call)

    hass.services.async_register("vacuum", "clean_area", handle)
    hass.states.async_set(
        "vacuum.test",
        "docked",
        {
            "supported_features": int(VacuumEntityFeature.CLEAN_AREA),
            "battery_level": 75,
        },
    )
    hass.states.async_set("sensor.start", "2026-09-07T10:00:00+00:00")
    hass.states.async_set("sensor.end", "2026-09-07T10:10:00+00:00")
    adapter = _adapter(hass)
    unit = _unit()
    assignment = DispatchAssignment(
        "unit",
        "robot",
        "source",
        "roborock",
        ("kitchen",),
        adapter.profile.capabilities.revision,
        PreferenceResolution((), ()),
    )

    await adapter.async_dispatch(unit, assignment)
    observation = await adapter.async_observe()

    assert calls[0].data == {
        "entity_id": "vacuum.test",
        "cleaning_area_id": ["kitchen"],
    }
    assert adapter.profile.capabilities.operations == frozenset(
        {OperationKind.VACUUM_AND_MOP}
    )
    assert observation.state is RobotAvailabilityState.AVAILABLE
    assert observation.battery_percentage == 75
    assert observation.history_start == datetime(2026, 9, 7, 10, tzinfo=UTC)
    assert adapter.watched_entity_ids == (
        "vacuum.test",
        "sensor.start",
        "sensor.end",
    )


async def test_busy_unknown_cancel_and_missing_features_are_normalized(
    hass: HomeAssistant,
) -> None:
    calls: list[ServiceCall] = []

    async def handle(call: ServiceCall) -> None:
        calls.append(call)

    hass.services.async_register("vacuum", "stop", handle)
    hass.states.async_set("vacuum.test", "cleaning", {"supported_features": 0})
    adapter = _adapter(hass)
    assert (await adapter.async_observe()).state is RobotAvailabilityState.BUSY
    await adapter.async_cancel()
    assert calls[0].data == {"entity_id": "vacuum.test"}
    assert adapter.profile.capabilities.operations == frozenset()

    hass.states.async_set("vacuum.test", "unavailable")
    assert (await adapter.async_observe()).state is RobotAvailabilityState.UNKNOWN


async def test_invalid_assignment_and_empty_targets_are_rejected(
    hass: HomeAssistant,
) -> None:
    adapter = _adapter(hass)
    unit = _unit()
    wrong = DispatchAssignment(
        "other", "robot", "source", "roborock", (), "caps", PreferenceResolution((), ())
    )
    with pytest.raises(ConflictError, match="assignment_work_unit_mismatch"):
        await adapter.async_dispatch(unit, wrong)
    empty = replace_assignment_work_unit(wrong, "unit")
    with pytest.raises(ConflictError, match="empty_adapter_targets"):
        await adapter.async_dispatch(unit, empty)


def replace_assignment_work_unit(
    assignment: DispatchAssignment, work_unit_id: str
) -> DispatchAssignment:
    return DispatchAssignment(
        work_unit_id,
        assignment.robot_id,
        assignment.source_robot_id,
        assignment.adapter,
        assignment.adapter_targets,
        assignment.capability_revision,
        assignment.preference_resolution,
    )
