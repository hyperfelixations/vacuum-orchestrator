"""A vacuum of any integration that cleans HA areas through `vacuum.clean_area`.

It has a device and an area mapping but no companion roles VOI knows; the
generic adapter drives it. Identities and names are synthetic.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from homeassistant.components.vacuum.const import VacuumEntityFeature
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import slugify
from pytest_homeassistant_custom_component.common import MockConfigEntry

from tests.realistic.actions import attach

FEATURES = (
    VacuumEntityFeature.STATE
    | VacuumEntityFeature.START
    | VacuumEntityFeature.STOP
    | VacuumEntityFeature.RETURN_HOME
    | VacuumEntityFeature.CLEAN_AREA
)
# Cleans the ground floor; the bath upstairs is not mapped.
MAPPING = {"Küche": ["kitchen"], "Flur": ["hall"], "Wohnzimmer": ["living"]}


@dataclass
class AreaVacuum:
    """The registered vacuum and the actions it received."""

    hass: HomeAssistant
    name: str
    entry: MockConfigEntry
    device: dr.DeviceEntry
    vacuum: er.RegistryEntry
    calls: list[ServiceCall] = field(default_factory=list)

    @property
    def entity_id(self) -> str:
        """The vacuum entity."""
        return self.vacuum.entity_id

    @classmethod
    def install(
        cls, hass: HomeAssistant, name: str, areas: dict[str, str]
    ) -> AreaVacuum:
        """Register the vacuum with its device and mapping; return it docked."""
        entry = MockConfigEntry(domain="mqtt", title="MQTT")
        entry.add_to_hass(hass)
        device = dr.async_get(hass).async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={("mqtt", f"{slugify(name)}_vacuum")},
            name=name,
            manufacturer="Valetudo",
            model="Generic area vacuum",
        )
        registry = er.async_get(hass)
        vacuum = registry.async_get_or_create(
            "vacuum",
            "mqtt",
            f"{slugify(name)}_vacuum",
            config_entry=entry,
            device_id=device.id,
            has_entity_name=True,
            original_name=None,
            suggested_object_id=slugify(name),
        )
        registry.async_update_entity_options(
            vacuum.entity_id,
            "vacuum",
            {
                "area_mapping": {
                    areas[area]: segments for area, segments in MAPPING.items()
                }
            },
        )
        robot = cls(hass, name, entry, device, registry.async_get(vacuum.entity_id))
        attach(
            hass,
            robot,
            [("vacuum", action) for action in ("clean_area", "stop", "return_to_base")],
        )
        robot.set_vacuum("docked")
        return robot

    def owns(self, entity_id: str) -> bool:
        """Whether the entity is this vacuum."""
        return entity_id == self.entity_id

    async def handle(self, call: ServiceCall) -> None:
        """Record the call."""
        self.calls.append(call)

    def set_vacuum(self, state: str) -> None:
        """Set the vacuum activity."""
        self.hass.states.async_set(
            self.entity_id, state, {"supported_features": int(FEATURES)}
        )
