"""Every Roborock fault option has a scope; see dev doc "Gerätefehler"."""

import json
from pathlib import Path

import homeassistant.components
import pytest
from homeassistant.core import HomeAssistant

from custom_components.vacuum_orchestrator.adapters.roborock_faults import (
    DOCK_FAULTS,
    DOCK_NEUTRAL,
    ROBOT_FAULTS,
    ROBOT_NEUTRAL,
    SENSOR_FAULTS,
)
from custom_components.vacuum_orchestrator.domain.faults import (
    Fault,
    FaultScope,
    FaultSource,
)
from custom_components.vacuum_orchestrator.domain.types import RobotAvailabilityState
from tests.adapters.test_roborock import setup_robot

CORE = Path(homeassistant.components.__file__).parent / "roborock" / "strings.json"


def _options(sensor: str) -> set[str]:
    strings = json.loads(CORE.read_text(encoding="utf-8"))
    return set(strings["entity"]["sensor"][sensor]["state"])


def test_tables_cover_exactly_the_core_options() -> None:
    assert set(ROBOT_FAULTS) | ROBOT_NEUTRAL == _options("vacuum_error")
    assert set(DOCK_FAULTS) | DOCK_NEUTRAL == _options("dock_error")
    assert not set(ROBOT_FAULTS) & ROBOT_NEUTRAL
    assert not set(DOCK_FAULTS) & DOCK_NEUTRAL


def test_sensor_roles_are_core_binary_sensors() -> None:
    strings = json.loads(CORE.read_text(encoding="utf-8"))
    assert {row[0] for row in SENSOR_FAULTS} <= set(strings["entity"]["binary_sensor"])


@pytest.mark.parametrize(
    ("role", "value", "fault", "state"),
    [
        (
            "error",
            "main_brush_jammed",
            Fault(
                "main_brush_jammed",
                FaultSource.ROBOT,
                FaultScope.VACUUM,
                "sensor.error",
            ),
            RobotAvailabilityState.AVAILABLE,
        ),
        (
            "error",
            "audio_error",
            Fault("audio_error", FaultSource.ROBOT, FaultScope.NOTICE, "sensor.error"),
            RobotAvailabilityState.AVAILABLE,
        ),
        (
            "error",
            "future_code",
            Fault("future_code", FaultSource.ROBOT, FaultScope.GENERAL, "sensor.error"),
            RobotAvailabilityState.UNAVAILABLE,
        ),
        (
            "dock_error",
            "water_empty",
            Fault(
                "water_empty",
                FaultSource.DOCK,
                FaultScope.STATION_MOP,
                "sensor.dock_error",
            ),
            RobotAvailabilityState.AVAILABLE,
        ),
    ],
)
async def test_faults_are_classified_where_they_are_reported(
    hass: HomeAssistant,
    role: str,
    value: str,
    fault: Fault,
    state: RobotAvailabilityState,
) -> None:
    adapter, _, _ = setup_robot(hass)
    assert (await adapter.async_observe()).faults == ()

    hass.states.async_set(f"sensor.{role}", value)
    observed = await adapter.async_observe()
    assert observed.faults == (fault,)
    assert observed.state is state
    assert observed.normal_end is (state is RobotAvailabilityState.AVAILABLE)


async def test_an_offline_robot_is_unavailable_without_a_fault(
    hass: HomeAssistant,
) -> None:
    adapter, _, _ = setup_robot(hass)
    hass.states.async_set("sensor.status", "device_offline")
    observed = await adapter.async_observe()
    assert observed.state is RobotAvailabilityState.UNAVAILABLE
    assert observed.faults == ()
