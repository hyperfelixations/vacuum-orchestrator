"""Binary sensor roles of the simulated robot and its dock."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import RoborockConfigEntry
from .entity import RoleEntity, roles
from .robot import SimulatedRoborock


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RoborockConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add every binary sensor role."""
    robot = entry.runtime_data
    async_add_entities(
        RoleBinarySensor(robot, role) for role in roles(robot, "binary_sensor")
    )


class RoleBinarySensor(RoleEntity, BinarySensorEntity):
    """A running, connectivity or problem sensor."""

    def __init__(self, robot: SimulatedRoborock, role: dict[str, Any]) -> None:
        super().__init__(robot, role)
        self._attr_device_class = BinarySensorDeviceClass(role["device_class"])

    @property
    def is_on(self) -> bool:
        """`on` as the robot reports it."""
        return self.value == "on"
