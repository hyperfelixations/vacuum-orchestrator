"""A Roborock V1 robot with dock as the HA 2026.10 core integration registers it.

Unique IDs, translation keys, entity names, the dock device and the map images
follow `homeassistant/components/roborock`; `tests/realistic/test_core_parity.py`
checks them against the installed core. Identities and names are synthetic.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any, Literal

from homeassistant.components.vacuum.const import VacuumEntityFeature
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
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
MODEL = "roborock.vacuum.a70"
# Locally administered, so no real device has it.
MAC = "02:00:00:00:00:01"
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
    Role("sensor", "current_room", "current_room", "Current room", "Dining Room"),
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
        "Main Floor",
        category=EntityCategory.CONFIG,
        options=("Main Floor", "Lower Floor"),
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
    (0, "Main Floor", {"16": "Dining Room", "17": "Studio", "18": "Library"}),
    (1, "Lower Floor", {"16": "Conservatory"}),
)
# A map image's state: when Roborock last drew it, and a synthetic access token.
MAP_UPDATED = "2026-03-14T08:00:00+00:00"
MAP_TOKEN = "synthetic-map-token"
# HA area mapping as the vacuum entity options store it; `0_99` no longer exists.
MAPPING = {
    "Dining Room": ["0_16"],
    "Studio": ["0_17"],
    "Library": ["0_18", "0_99"],
    "Conservatory": ["1_16"],
}


# A run as a V1 robot with a mop-washing dock reports it: steps of
# (seconds after the previous step, changes). The order of the steps is how the
# core integration reports a run; every duration is synthetic and round. See
# dev doc "Testarchitektur".
type Step = tuple[float, dict[str, str]]
# Step values replaced by the time of the step or of the run's first step.
NOW = "<now>"
RUN_START = "<run start>"
# Durations, in whole half minutes.
SECTION_SECONDS = 600  # cleaning one section of rooms
TRAVEL_SECONDS = 60  # to the dock: home or to a mop wash
PREWASH_SECONDS = 120  # the dock washes the mop before the first section
WASH_SECONDS = 120  # the dock washes the mop between two sections
POLL_SECONDS = 30  # the run flag follows the status at the next poll
BLIP_AFTER_DOCK_SECONDS = 180  # mopping: the dock reports `returning` briefly
FLAG_AFTER_DOCK_SECONDS = 210  # mopping: the run flag ends after the last wash
# Short signals of a moment.
FLAG_AFTER_RETURN_SECONDS = 15  # vacuuming: the run flag ends on the way home
BLIP_SECONDS = 3
FAULT_FLAG_SECONDS = 2  # the robot flags the station's empty tank
TANK_REPORT_SECONDS = 10  # then the dock reports it
DURATIONS = (
    SECTION_SECONDS,
    TRAVEL_SECONDS,
    PREWASH_SECONDS,
    WASH_SECONDS,
    POLL_SECONDS,
    BLIP_AFTER_DOCK_SECONDS,
    FLAG_AFTER_DOCK_SECONDS,
)
SIGNALS = (
    FLAG_AFTER_RETURN_SECONDS,
    BLIP_SECONDS,
    FAULT_FLAG_SECONDS,
    TANK_REPORT_SECONDS,
)


def vacuum_run(sections: int = 1) -> tuple[Step, ...]:
    """Vacuuming: the run flag ends on the way home, before the robot docks."""
    return (
        (0, {"status": "segment_cleaning", "vacuum": "cleaning"}),
        (POLL_SECONDS, {"in_cleaning": "on"}),
        (
            SECTION_SECONDS * sections - POLL_SECONDS,
            {"status": "returning_home", "vacuum": "returning"},
        ),
        (
            FLAG_AFTER_RETURN_SECONDS,
            {
                "in_cleaning": "off",
                "last_clean_start": RUN_START,
                "last_clean_end": NOW,
            },
        ),
        (
            TRAVEL_SECONDS - FLAG_AFTER_RETURN_SECONDS,
            {"status": "charging", "vacuum": "docked"},
        ),
    )


def mop_run(
    sections: int = 1,
    *,
    prewash: float = PREWASH_SECONDS,
    empty_tank: Literal["during", "after"] | None = None,
) -> tuple[Step, ...]:
    """Mopping: the dock washes the mop before, between and after the sections.

    The run flag is on from the first wash and ends after the last; at the end
    the dock briefly reports `returning` and resets the cleaning mode. With
    `empty_tank` the dock runs out of clean water as the first section starts
    (`during`) or while it washes the mop after the run (`after`): the robot
    flags a station fault for a moment, the dock reports `water_empty` until
    refilled, and the robot goes on.
    """
    timeline: list[Step] = [(0, {"status": "washing_the_mop", "in_cleaning": "on"})]
    at = prewash
    for section in range(sections):
        if section:
            timeline += [
                (at, {"status": "going_to_wash_the_mop", "vacuum": "returning"}),
                (
                    at + TRAVEL_SECONDS,
                    {"status": "washing_the_mop", "vacuum": "docked"},
                ),
            ]
            at += TRAVEL_SECONDS + WASH_SECONDS
        timeline.append((at, {"status": "segment_cleaning", "vacuum": "cleaning"}))
        at += SECTION_SECONDS
    docked = at + TRAVEL_SECONDS
    blip = docked + BLIP_AFTER_DOCK_SECONDS
    ended = docked + FLAG_AFTER_DOCK_SECONDS
    timeline += [
        (at, {"status": "returning_home", "vacuum": "returning"}),
        (docked, {"status": "charging", "vacuum": "docked"}),
        (blip, {"vacuum": "returning"}),
        (blip + BLIP_SECONDS, {"vacuum": "docked", "cleaning_mode": "vac_and_mop"}),
        (
            ended,
            {
                "in_cleaning": "off",
                "last_clean_start": RUN_START,
                "last_clean_end": NOW,
            },
        ),
    ]
    if empty_tank is not None:
        flagged = prewash - FAULT_FLAG_SECONDS if empty_tank == "during" else blip
        empty = prewash + TANK_REPORT_SECONDS if empty_tank == "during" else ended
        timeline += [
            (flagged, {"vacuum_error": "clear_water_box_hoare"}),
            (flagged + FAULT_FLAG_SECONDS, {"vacuum_error": "none"}),
            (empty, {"dock_error": "water_empty"}),
        ]
    return _steps(timeline)


def _steps(timeline: list[Step]) -> tuple[Step, ...]:
    """Merge the changes of one moment and turn times into delays."""
    moments: dict[float, dict[str, str]] = {}
    for at, changes in sorted(timeline, key=lambda step: step[0]):
        moments.setdefault(at, {}).update(changes)
    return tuple(
        (at - previous, moments[at]) for previous, at in pairwise([0, *sorted(moments)])
    )


def duid_of(name: str) -> str:
    """The synthetic device ID of a robot with this name."""
    return f"{slugify(name)}0000000000duid"


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
        duid = duid_of(name)
        slug = slugify(duid)
        entry = MockConfigEntry(domain="roborock", title="Roborock")
        entry.add_to_hass(hass)
        devices = dr.async_get(hass)
        device = devices.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={("roborock", duid)},
            name=name,
            manufacturer="Roborock",
            model=MODEL,
            model_id=MODEL,
            connections={(dr.CONNECTION_NETWORK_MAC, MAC)},
        )
        dock = devices.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={("roborock", f"{duid}_dock")},
            name=f"{name} Dock",
            manufacturer="Roborock",
            model=f"{MODEL} Dock",
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
        for map_name, image in images.items():
            hass.states.async_set(
                image.entity_id,
                MAP_UPDATED,
                {
                    "access_token": MAP_TOKEN,
                    "entity_picture": f"/api/image_proxy/{image.entity_id}"
                    f"?token={MAP_TOKEN}",
                    "friendly_name": f"{name} {map_name}",
                },
            )
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
        """Clean, as the robot reports any run: in the room of the first segment
        it was last sent to, on the selected map."""
        self.set("status", status)
        self.set("in_cleaning", "on")
        if commands := self.segment_commands():
            selected = self.hass.states.get(self.entities["selected_map"].entity_id)
            rooms = next(
                (
                    rooms
                    for _, name, rooms in MAPS
                    if selected and name == selected.state
                ),
                MAPS[0][2],
            )
            self.set("current_room", rooms[str(commands[-1]["segments"][0])])
        self.set_vacuum("cleaning")

    def stop(self) -> None:
        """Stop where the robot is, as after a stop command."""
        self.set("status", "idle")
        self.set("in_cleaning", "off")
        self.set_vacuum("idle")

    def fail(self, code: str) -> None:
        """Stop with a fault, as the robot reports one."""
        self.set("vacuum_error", code)
        self.set("status", "error")
        self.set_vacuum("error")

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

    async def play(
        self,
        script: tuple[Step, ...],
        advance: Callable[[float], Awaitable[None]],
        reported: Callable[[dict[str, str]], Awaitable[None]] | None = None,
    ) -> None:
        """Report each step once `advance` let its delay pass, then `reported`."""
        start = dt_util.utcnow().isoformat()
        for delay, changes in script:
            if delay:
                await advance(delay)
            now = dt_util.utcnow().isoformat()
            for key, reported_value in changes.items():
                value = {NOW: now, RUN_START: start}.get(reported_value, reported_value)
                if key == "vacuum":
                    self.set_vacuum(value)
                else:
                    self.set(key, value)
            if reported is not None:
                await reported(changes)

    def segment_commands(self) -> list[dict[str, Any]]:
        """The `app_segment_clean` parameters sent so far."""
        return [
            call.data["params"][0]
            for call in self.calls
            if call.service == "send_command"
            and call.data["command"] == "app_segment_clean"
        ]
