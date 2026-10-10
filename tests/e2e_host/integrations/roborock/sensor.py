"""Sensor roles of the simulated robot and its dock."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import PERCENTAGE
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
    """Add every sensor role."""
    robot = entry.runtime_data
    async_add_entities(RoleSensor(robot, role) for role in roles(robot, "sensor"))


class RoleSensor(RoleEntity, SensorEntity):
    """An enum, battery, timestamp or plain sensor."""

    def __init__(self, robot: SimulatedRoborock, role: dict[str, Any]) -> None:
        super().__init__(robot, role)
        device_class = role["device_class"]
        self._attr_device_class = device_class and SensorDeviceClass(device_class)
        if role["options"]:
            self._attr_options = list(role["options"])
        if device_class == SensorDeviceClass.BATTERY:
            self._attr_native_unit_of_measurement = PERCENTAGE

    @property
    def native_value(self) -> str | datetime | None:
        """Timestamps as datetimes, everything else as reported."""
        if self.device_class is SensorDeviceClass.TIMESTAMP:
            return datetime.fromisoformat(self.value)
        return self.value
