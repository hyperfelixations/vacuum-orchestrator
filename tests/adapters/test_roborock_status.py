"""Every Roborock status has a phase; see dev doc "Gerätezustand"."""

import pytest
from homeassistant.core import HomeAssistant

from custom_components.vacuum_orchestrator.adapters.roborock_status import (
    STATUS_PHASES,
)
from custom_components.vacuum_orchestrator.domain.types import (
    RobotAvailabilityState,
    RobotPhase,
)
from tests.adapters.test_roborock import setup_robot
from tests.adapters.test_roborock_faults import _options


def test_the_table_covers_exactly_the_core_options() -> None:
    assert set(STATUS_PHASES) == _options("status")


@pytest.mark.parametrize(
    ("status", "phase", "cleaning", "state", "at_dock"),
    [
        ("sweeping", RobotPhase.CLEANING, True, RobotAvailabilityState.BUSY, False),
        ("mopping", RobotPhase.CLEANING, True, RobotAvailabilityState.BUSY, False),
        (
            "sweep_and_mop",
            RobotPhase.CLEANING,
            True,
            RobotAvailabilityState.BUSY,
            False,
        ),
        (
            "returning_home",
            RobotPhase.RETURNING,
            False,
            RobotAvailabilityState.BUSY,
            False,
        ),
        (
            "emptying_the_bin",
            RobotPhase.STATION,
            False,
            RobotAvailabilityState.BUSY,
            True,
        ),
        ("charging", RobotPhase.DOCKED, False, RobotAvailabilityState.AVAILABLE, True),
        ("sleeping", RobotPhase.IDLE, False, RobotAvailabilityState.AVAILABLE, False),
        ("mapping", RobotPhase.OTHER, False, RobotAvailabilityState.BUSY, False),
        (
            "not_an_option",
            RobotPhase.UNKNOWN,
            False,
            RobotAvailabilityState.BUSY,
            False,
        ),
    ],
)
async def test_status_sets_phase_activity_and_dock(
    hass: HomeAssistant,
    status: str,
    phase: RobotPhase,
    cleaning: bool,
    state: RobotAvailabilityState,
    at_dock: bool,
) -> None:
    adapter, _, _calls = setup_robot(hass)
    hass.states.async_set("sensor.status", status)

    observation = await adapter.async_observe()

    assert observation.phase is phase
    assert observation.cleaning_active is cleaning
    assert (observation.state, observation.at_dock) == (state, at_dock)
    assert observation.normal_end is (state is RobotAvailabilityState.AVAILABLE)
