"""Select roles of the simulated robot; a selection takes effect at once."""

from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity
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
    """Add every select role."""
    robot = entry.runtime_data
    async_add_entities(RoleSelect(robot, role) for role in roles(robot, "select"))


class RoleSelect(RoleEntity, SelectEntity):
    """Mop intensity, mop mode, cleaning mode or selected map."""

    def __init__(self, robot: SimulatedRoborock, role: dict[str, Any]) -> None:
        super().__init__(robot, role)
        self._attr_options = list(role["options"])

    @property
    def current_option(self) -> str:
        """The option the robot reports."""
        return self.value

    async def async_select_option(self, option: str) -> None:
        """Send the option to the robot."""
        await self.robot.command("set_option", {self.key: option})
        self.robot.set_role(self.key, option)
