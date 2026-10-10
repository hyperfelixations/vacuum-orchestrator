"""The simulated area vacuum; `vacuum.clean_area` reaches it as segments."""

from __future__ import annotations

from typing import Any

from custom_components.voi_e2e_support.simulation import (
    CLEAN_SECONDS_PER_TARGET,
    SimulatedRobot,
    map_areas,
)
from homeassistant.components.vacuum import (
    Segment,
    StateVacuumEntity,
    VacuumActivity,
    VacuumEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import slugify

DOMAIN = "voi_sim"


class SimulatedAreaRobot(SimulatedRobot):
    """Cleans segments; each mapped HA area is one segment named after it."""

    def __init__(self, spec: dict[str, Any]) -> None:
        super().__init__(spec["name"])
        self.spec = spec

    def execute(self, name: str, params: Any) -> None:
        """Carry out a vacuum action."""
        match name:
            case "clean_segments":
                self.clean(CLEAN_SECONDS_PER_TARGET * len(params))
            case "start":
                self.clean(CLEAN_SECONDS_PER_TARGET * len(self.spec["mapping"]))
            case "stop":
                self.stop()
            case "return_to_base":
                self.return_home()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry[SimulatedAreaRobot],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the vacuum."""
    async_add_entities([SimulatedAreaVacuum(entry.runtime_data)])


class SimulatedAreaVacuum(StateVacuumEntity):
    """Named after its device."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_should_poll = False

    def __init__(self, robot: SimulatedAreaRobot) -> None:
        self.robot = robot
        slug = f"{slugify(robot.name)}_vacuum"
        self._attr_unique_id = slug
        self._attr_supported_features = VacuumEntityFeature(robot.spec["features"])
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, slug)},
            name=robot.name,
            manufacturer="VOI Simulator",
            model="Generic area vacuum",
        )

    @property
    def available(self) -> bool:
        """Whether the robot is connected."""
        return self.robot.available

    @property
    def activity(self) -> VacuumActivity:
        """The robot's activity."""
        return VacuumActivity(self.robot.activity.value)

    async def async_added_to_hass(self) -> None:
        """Follow the robot; map the HA areas once, as a user does."""
        self.async_on_remove(self.robot.listen(self.async_write_ha_state))
        map_areas(self.hass, self.entity_id, self.robot.spec["mapping"])

    async def async_start(self) -> None:
        """Clean every mapped area."""
        await self.robot.command("start")

    async def async_stop(self, **kwargs: Any) -> None:
        """Stop."""
        await self.robot.command("stop")

    async def async_return_to_base(self, **kwargs: Any) -> None:
        """Drive home."""
        await self.robot.command("return_to_base")

    async def async_get_segments(self) -> list[Segment]:
        """One segment per mapped area."""
        return [
            Segment(id=segment, name=name)
            for name, segments in self.robot.spec["mapping"].items()
            for segment in segments
        ]

    async def async_clean_segments(self, segment_ids: list[str], **kwargs: Any) -> None:
        """Clean the segments HA resolved from the areas."""
        await self.robot.command("clean_segments", list(segment_ids))
