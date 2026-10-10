"""SIMULATOR replacing the core Roborock integration in the throwaway E2E instance.

It registers what core 2026.10 registers for a V1 robot with dock (devices,
unique IDs, translation keys, map images, `roborock.get_maps`) and answers
commands from a simulated robot. The household comes from
`tests/realistic/roborock.py`; see dev doc "E2E-Host".
"""

from __future__ import annotations

from custom_components.voi_e2e_support.simulation import robots
from homeassistant.components.vacuum import DOMAIN as VACUUM_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import service
from homeassistant.helpers.typing import ConfigType

from .robot import SimulatedRoborock

DOMAIN = "roborock"
PLATFORMS = [
    Platform.VACUUM,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SELECT,
    Platform.IMAGE,
]

type RoborockConfigEntry = ConfigEntry[SimulatedRoborock]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register `roborock.get_maps` as core does."""
    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        "get_maps",
        entity_domain=VACUUM_DOMAIN,
        schema=None,
        func="get_maps",
        supports_response=SupportsResponse.ONLY,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: RoborockConfigEntry) -> bool:
    """Register the robot and its dock, then its entities."""
    robot = SimulatedRoborock(dict(entry.data))
    spec = robot.spec
    devices = dr.async_get(hass)
    devices.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, spec["duid"])},
        name=spec["name"],
        manufacturer="Roborock",
        model=spec["model"],
        model_id=spec["model"],
        connections={(dr.CONNECTION_NETWORK_MAC, spec["mac"])},
    )
    devices.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{spec['duid']}_dock")},
        name=f"{spec['name']} Dock",
        manufacturer="Roborock",
        model=f"{spec['model']} Dock",
    )
    if spec["cleaning"]:
        robot.clean()
    entry.runtime_data = robot
    robots(hass)[robot.name] = robot
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: RoborockConfigEntry) -> bool:
    """Remove the entities; the simulated robot goes with them."""
    robots(hass).pop(entry.runtime_data.name, None)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
