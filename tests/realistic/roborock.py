"""A Roborock V1 robot with dock as the HA 2026.10 core integration registers it.

Unique IDs, translation keys, entity names, the dock device and the map images
follow `homeassistant/components/roborock`; `tests/realistic/test_core_parity.py`
checks them against the installed core. Identities and names are synthetic.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from homeassistant.components.vacuum.const import VacuumEntityFeature
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import slugify
from pytest_homeassistant_custom_component.common import MockConfigEntry

from tests.realistic.actions import attach, targets

FEATURES = (
    VacuumEntityFeature.PAUSE
    | VacuumEntityFeature.STOP
    | VacuumEntityFeature.RETURN_HOME
    | VacuumEntityFeature.FAN_SPEED
    | VacuumEntityFeature.SEND_COMMAND
    | VacuumEntityFeature.LOCATE
    | VacuumEntityFeature.CLEAN_SPOT
    | VacuumEntityFeature.STATE
    | VacuumEntityFeature.START
    | VacuumEntityFeature.CLEAN_AREA
)
FAN_SPEEDS = ["off", "quiet", "balanced", "turbo", "max", "max_plus", "custom"]
STATUS_OPTIONS = [
    "air_drying_stopping",
    "attaching_the_mop",
    "charger_disconnected",
    "charging",
    "charging_complete",
    "charging_problem",
    "cleaning",
    "detaching_the_mop",
    "device_offline",
    "docking",
    "egg_attack",
    "emptying_the_bin",
    "error",
    "going_to_target",
    "going_to_wash_the_mop",
    "idle",
    "locked",
    "manual_mode",
    "mapping",
    "mopping",
    "paused",
    "relocating",
    "remote_control_active",
    "returning_home",
    "saving_map",
    "segment_cleaning",
    "shutting_down",
    "sleeping",
    "spot_cleaning",
    "starting",
    "sweep_and_mop",
    "sweeping",
    "transitioning",
    "unknown",
    "updating",
    "waiting_to_charge",
    "washing_the_mop",
    "zoned_cleaning",
]


@dataclass(frozen=True)
class Role:
    """One companion entity: `(domain, key)` makes the unique ID `<key>_<slug>`."""

    domain: str
    key: str
    translation_key: str | None
    name: str
    state: str
    dock: bool = False
    device_class: str | None = None
    category: EntityCategory = EntityCategory.DIAGNOSTIC
    options: tuple[str, ...] = ()


ROLES = (
    Role(
        "sensor",
        "status",
        "status",
        "Status",
        "charging",
        device_class="enum",
        options=tuple(STATUS_OPTIONS),
    ),
    Role(
        "sensor",
        "vacuum_error",
        "vacuum_error",
        "Vacuum error",
        "none",
        device_class="enum",
    ),
    Role(
        "sensor",
        "dock_error",
        "dock_error",
        "Dock error",
        "ok",
        dock=True,
        device_class="enum",
    ),
    Role("sensor", "battery", None, "Battery", "100", device_class="battery"),
    Role(
        "sensor",
        "last_clean_start",
        "last_clean_start",
        "Last clean begin",
        "2026-03-14T08:00:00+00:00",
        device_class="timestamp",
    ),
    Role(
        "sensor",
        "last_clean_end",
        "last_clean_end",
        "Last clean end",
        "2026-03-14T08:40:00+00:00",
        device_class="timestamp",
    ),
    Role("sensor", "clean_percent", "clean_percent", "Cleaning progress", "0"),
    Role("sensor", "current_room", "current_room", "Current room", "Küche"),
    Role(
        "select",
        "water_box_mode",
        "mop_intensity",
        "Mop intensity",
        "moderate",
        category=EntityCategory.CONFIG,
        options=("off", "mild", "moderate", "intense", "custom"),
    ),
    Role(
        "select",
        "mop_mode",
        "mop_mode",
        "Mop mode",
        "standard",
        category=EntityCategory.CONFIG,
        options=("standard", "deep", "deep_plus", "fast", "custom"),
    ),
    Role(
        "select",
        "cleaning_mode",
        "cleaning_mode",
        "Cleaning mode",
        "vac_and_mop",
        category=EntityCategory.CONFIG,
        options=("vacuum", "mop", "vac_and_mop"),
    ),
    Role(
        "select",
        "selected_map",
        "selected_map",
        "Selected map",
        "Erdgeschoss",
        category=EntityCategory.CONFIG,
        options=("Erdgeschoss", "Obergeschoss"),
    ),
    Role(
        "binary_sensor",
        "in_cleaning",
        "in_cleaning",
        "Cleaning",
        "off",
        device_class="running",
    ),
    Role(
        "binary_sensor",
        "water_box_carriage_status",
        "mop_attached",
        "Mop attached",
        "on",
        device_class="connectivity",
    ),
    Role(
        "binary_sensor",
        "water_box_status",
        "water_box_attached",
        "Water box attached",
        "on",
        device_class="connectivity",
    ),
    Role(
        "binary_sensor",
        "water_shortage",
        "water_shortage",
        "Water shortage",
        "off",
        device_class="problem",
    ),
    Role(
        "binary_sensor",
        "dirty_box_full",
        "dirty_box_full",
        "Dirty water box",
        "off",
        dock=True,
        device_class="problem",
    ),
    Role(
        "binary_sensor",
        "clean_box_empty",
        "clean_box_empty",
        "Clean water box",
        "off",
        dock=True,
        device_class="problem",
    ),
    Role(
        "binary_sensor",
        "clean_fluid_empty",
        "clean_fluid_empty",
        "Cleaning fluid",
        "off",
        dock=True,
        device_class="problem",
    ),
)
# Map flag, name and rooms as `roborock.get_maps` returns them.
MAPS = (
    (0, "Erdgeschoss", {"16": "Küche", "17": "Flur", "18": "Wohnzimmer"}),
    (1, "Obergeschoss", {"16": "Bad"}),
)
# HA area mapping as the vacuum entity options store it; `0_99` no longer exists.
MAPPING = {
    "Küche": ["0_16"],
    "Flur": ["0_17"],
    "Wohnzimmer": ["0_18", "0_99"],
    "Bad": ["1_16"],
}


@dataclass
class RoborockV1:
    """The registered robot, its recorded actions and its live state."""

    hass: HomeAssistant
    name: str
    duid: str
    entry: MockConfigEntry
    device: dr.DeviceEntry
    dock: dr.DeviceEntry
    vacuum: er.RegistryEntry
    entities: dict[str, er.RegistryEntry]
    images: dict[str, er.RegistryEntry]
    calls: list[ServiceCall] = field(default_factory=list)
    # Set while a delayed command waits for its answer.
    waiting: asyncio.Event = field(default_factory=asyncio.Event)
    _answer: asyncio.Event | None = None

    @property
    def entity_id(self) -> str:
        """The vacuum entity."""
        return self.vacuum.entity_id

    def delay_commands(self) -> asyncio.Event:
        """Answer the next command only once the returned event is set."""
        self._answer = asyncio.Event()
        self.waiting.clear()
        return self._answer

    @classmethod
    def install(
        cls, hass: HomeAssistant, name: str, areas: dict[str, str]
    ) -> RoborockV1:
        """Register the robot, its dock, entities and actions; return it docked."""
        duid = f"{slugify(name)}0000000000duid"
        slug = slugify(duid)
        entry = MockConfigEntry(domain="roborock", title="Roborock")
        entry.add_to_hass(hass)
        devices = dr.async_get(hass)
        device = devices.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={("roborock", duid)},
            name=name,
            manufacturer="Roborock",
            model="roborock.vacuum.a70",
            model_id="roborock.vacuum.a70",
            connections={(dr.CONNECTION_NETWORK_MAC, "02:00:00:00:00:01")},
        )
        dock = devices.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={("roborock", f"{duid}_dock")},
            name=f"{name} Dock",
            manufacturer="Roborock",
            model="roborock.vacuum.a70 Dock",
        )
        registry = er.async_get(hass)
        prefix = slugify(name)
        vacuum = registry.async_get_or_create(
            "vacuum",
            "roborock",
            slug,
            config_entry=entry,
            device_id=device.id,
            has_entity_name=True,
            original_name=None,
            translation_key="roborock",
            suggested_object_id=prefix,
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
        vacuum = registry.async_get(vacuum.entity_id)
        entities = {}
        for role in ROLES:
            owner = dock if role.dock else device
            entities[role.key] = registry.async_get_or_create(
                role.domain,
                "roborock",
                f"{role.key}_{slug}",
                config_entry=entry,
                device_id=owner.id,
                has_entity_name=True,
                original_name=None if role.translation_key else role.name,
                translation_key=role.translation_key,
                original_device_class=role.device_class,
                entity_category=role.category,
                suggested_object_id=f"{slugify(owner.name)}_{slugify(role.name)}",
            )
        images = {
            map_name: registry.async_get_or_create(
                "image",
                "roborock",
                f"{slug}_map_{map_name}",
                config_entry=entry,
                device_id=device.id,
                has_entity_name=True,
                original_name=map_name,
                entity_category=EntityCategory.DIAGNOSTIC,
                suggested_object_id=f"{prefix}_{slugify(map_name)}",
            )
            for _flag, map_name, _rooms in MAPS
        }
        robot = cls(hass, name, duid, entry, device, dock, vacuum, entities, images)
        attach(
            hass,
            robot,
            [
                ("vacuum", action)
                for action in (
                    "send_command",
                    "stop",
                    "return_to_base",
                    "set_fan_speed",
                )
            ]
            + [("select", "select_option")],
            responding=[("roborock", "get_maps")],
        )
        for role in ROLES:
            robot.set(role.key, role.state)
        robot.set_vacuum("docked")
        return robot

    def owns(self, entity_id: str) -> bool:
        """Whether the entity is the vacuum or one of its companions."""
        return entity_id == self.entity_id or any(
            item.entity_id == entity_id for item in self.entities.values()
        )

    async def handle(self, call: ServiceCall) -> dict[str, Any] | None:
        """Record the call; answer `get_maps` and apply settings."""
        self.calls.append(call)
        if call.service == "send_command" and self._answer is not None:
            self.waiting.set()
            await self._answer.wait()
            self._answer = None
        if call.service == "get_maps":
            return {
                self.entity_id: {
                    "maps": [
                        {"flag": flag, "name": name, "rooms": dict(rooms)}
                        for flag, name, rooms in MAPS
                    ]
                }
            }
        if call.service == "set_fan_speed":
            self.set_vacuum(None, fan_speed=call.data["fan_speed"])
        elif call.service == "select_option":
            for entity_id in targets(call):
                key = next(
                    key
                    for key, item in self.entities.items()
                    if item.entity_id == entity_id
                )
                self.set(key, call.data["option"])
        return None

    def set(self, key: str, value: str) -> None:
        """Set a companion entity's state, keeping its attributes."""
        role = next(item for item in ROLES if item.key == key)
        attributes: dict[str, Any] = (
            {"options": list(role.options)} if role.options else {}
        )
        if role.device_class == "battery":
            attributes["unit_of_measurement"] = "%"
        self.hass.states.async_set(self.entities[key].entity_id, value, attributes)

    def set_vacuum(self, state: str | None, *, fan_speed: str | None = None) -> None:
        """Set the vacuum activity; `None` keeps it."""
        current = self.hass.states.get(self.entity_id)
        attributes = {
            "supported_features": int(FEATURES),
            "fan_speed_list": FAN_SPEEDS,
            "fan_speed": fan_speed
            or (current.attributes.get("fan_speed") if current else "balanced"),
        }
        self.hass.states.async_set(
            self.entity_id,
            state if state is not None else (current.state if current else "docked"),
            attributes,
        )

    def clean(self, status: str = "segment_cleaning") -> None:
        """Clean, as the robot reports any run."""
        self.set("status", status)
        self.set("in_cleaning", "on")
        self.set_vacuum("cleaning")

    def pause(self) -> None:
        """Interrupt the run where the robot is."""
        self.set("status", "paused")
        self.set_vacuum("paused")

    def return_home(self) -> None:
        """Drive home after a run."""
        self.set("status", "returning_home")
        self.set("in_cleaning", "off")
        self.set_vacuum("returning")

    def dock_after_run(self) -> None:
        """Rest at the dock and charge."""
        self.set("status", "charging")
        self.set("in_cleaning", "off")
        self.set_vacuum("docked")

    def segment_commands(self) -> list[dict[str, Any]]:
        """The `app_segment_clean` parameters sent so far."""
        return [
            call.data["params"][0]
            for call in self.calls
            if call.service == "send_command"
            and call.data["command"] == "app_segment_clean"
        ]
