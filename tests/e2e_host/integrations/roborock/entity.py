"""Entities of the simulated robot; see dev doc "E2E-Host"."""

from __future__ import annotations

from typing import Any

from homeassistant.const import EntityCategory
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .robot import SimulatedRoborock

DOMAIN = "roborock"


class SimulatedEntity(Entity):
    """Follows the simulated robot; unavailable while it is offline."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, robot: SimulatedRoborock, *, dock: bool = False) -> None:
        self.robot = robot
        duid = robot.spec["duid"]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{duid}_dock" if dock else duid)}
        )

    @property
    def available(self) -> bool:
        """Whether the robot is connected."""
        return self.robot.available

    async def async_added_to_hass(self) -> None:
        """Write the state after every change of the robot."""
        self.async_on_remove(self.robot.listen(self.async_write_ha_state))


class RoleEntity(SimulatedEntity):
    """A companion entity named and identified as core does."""

    def __init__(self, robot: SimulatedRoborock, role: dict[str, Any]) -> None:
        super().__init__(robot, dock=role["dock"])
        self.key = role["key"]
        self._attr_unique_id = f"{self.key}_{robot.slug}"
        self._attr_entity_category = EntityCategory(role["category"])
        if role["translation_key"] is None:
            self._attr_name = role["name"]
        else:
            self._attr_translation_key = role["translation_key"]

    @property
    def value(self) -> str:
        """The role's current value."""
        return self.robot.roles[self.key]


def roles(robot: SimulatedRoborock, domain: str) -> list[dict[str, Any]]:
    """The robot's roles on one platform."""
    return [role for role in robot.spec["roles"] if role["domain"] == domain]
