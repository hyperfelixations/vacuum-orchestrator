"""The simulated V1 vacuum with the features and actions of core's."""

from __future__ import annotations

from typing import Any

from custom_components.voi_e2e_support.simulation import map_areas
from homeassistant.components.vacuum import (
    Segment,
    StateVacuumEntity,
    VacuumActivity,
    VacuumEntityFeature,
)
from homeassistant.core import HomeAssistant, ServiceResponse
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DOMAIN, RoborockConfigEntry
from .entity import SimulatedEntity
from .robot import SimulatedRoborock


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RoborockConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the vacuum."""
    async_add_entities([SimulatedVacuum(entry.runtime_data)])


class SimulatedVacuum(SimulatedEntity, StateVacuumEntity):
    """Named after its device; segments are `<map flag>_<room>`."""

    _attr_translation_key = DOMAIN
    _attr_name = None

    def __init__(self, robot: SimulatedRoborock) -> None:
        super().__init__(robot)
        self._attr_unique_id = robot.slug
        self._attr_supported_features = VacuumEntityFeature(robot.spec["features"])
        self._attr_fan_speed_list = list(robot.spec["fan_speeds"])

    async def async_added_to_hass(self) -> None:
        """Map the HA areas once, as a user does in the area mapping dialog."""
        await super().async_added_to_hass()
        map_areas(self.hass, self.entity_id, self.robot.spec["mapping"])

    @property
    def activity(self) -> VacuumActivity:
        """The robot's activity."""
        return VacuumActivity(self.robot.activity.value)

    @property
    def fan_speed(self) -> str:
        """The robot's fan speed."""
        return self.robot.fan_speed

    async def async_start(self) -> None:
        """Start or resume."""
        await self.robot.command("app_start")

    async def async_pause(self) -> None:
        """Pause."""
        await self.robot.command("app_pause")

    async def async_stop(self, **kwargs: Any) -> None:
        """Stop."""
        await self.robot.command("app_stop")

    async def async_return_to_base(self, **kwargs: Any) -> None:
        """Drive home."""
        await self.robot.command("app_charge")

    async def async_clean_spot(self, **kwargs: Any) -> None:
        """Spot clean, recorded only."""
        await self.robot.command("app_spot")

    async def async_locate(self, **kwargs: Any) -> None:
        """Locate, recorded only."""
        await self.robot.command("find_me")

    async def async_set_fan_speed(self, fan_speed: str, **kwargs: Any) -> None:
        """Set the fan speed."""
        await self.robot.command("set_custom_mode", [fan_speed])

    async def async_send_command(
        self,
        command: str,
        params: dict[str, Any] | list[Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Send a raw V1 command."""
        await self.robot.command(command, params)

    async def async_get_segments(self) -> list[Segment]:
        """Every room of every map, grouped by map."""
        return [
            Segment(id=segment, name=name, group=group)
            for segment, name, group in self.robot.segments()
        ]

    async def async_clean_segments(self, segment_ids: list[str], **kwargs: Any) -> None:
        """Clean the segments of the current map; others are ignored as in core."""
        current = [
            int(room)
            for flag, room in (segment.split("_", 1) for segment in segment_ids)
            if int(flag) == self.robot.current_map
        ]
        if current:
            await self.robot.command("app_segment_clean", [{"segments": current}])

    async def get_maps(self) -> ServiceResponse:
        """Maps and their rooms; room keys are integers as in core."""
        return {
            "maps": [
                {
                    "flag": item["flag"],
                    "name": item["name"],
                    "rooms": {int(room): name for room, name in item["rooms"].items()},
                }
                for item in self.robot.spec["maps"]
            ]
        }
