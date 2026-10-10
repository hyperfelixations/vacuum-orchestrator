"""SIMULATOR of a vacuum of any integration that cleans HA areas.

Registered like `tests/realistic/generic.py`: one device, a vacuum with
`CLEAN_AREA` and an area mapping, no companion roles. Exists only in the
throwaway E2E instance; see dev doc "E2E-Host".
"""

from __future__ import annotations

from custom_components.voi_e2e_support.simulation import robots
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .vacuum import SimulatedAreaRobot

DOMAIN = "voi_sim"
PLATFORMS = [Platform.VACUUM]

type SimConfigEntry = ConfigEntry[SimulatedAreaRobot]


async def async_setup_entry(hass: HomeAssistant, entry: SimConfigEntry) -> bool:
    """Create the simulated robot and its vacuum."""
    robot = SimulatedAreaRobot(dict(entry.data))
    entry.runtime_data = robot
    robots(hass)[robot.name] = robot
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SimConfigEntry) -> bool:
    """Remove the vacuum; the simulated robot goes with it."""
    robots(hass).pop(entry.runtime_data.name, None)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
