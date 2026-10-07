"""Roborock public-entity semantics and map-scoped V1 segment commands."""

import json
from collections.abc import Mapping
from dataclasses import replace
from hashlib import sha256
from typing import Any

from homeassistant.components.vacuum.const import VacuumEntityFeature

from ..domain.capabilities import AreaAddressing, PassCapability, RobotProfile
from ..domain.dispatching import RobotObservation
from ..domain.errors import ConflictError, DispatchNotStartedError, OrchestratorError
from ..domain.planning import DispatchAssignment, WorkUnit
from ..domain.reach import ReachStatus, RoomReach
from ..domain.rooms import RoomBinding
from ..domain.types import PassScope, RobotAvailabilityState
from ..ha_context import physical_context
from ..ports.telemetry import TelemetryEvent, report_adapter
from .home_assistant_vacuum import HomeAssistantVacuumAdapter
from .settings import available_options, supported_mapping

_MODE_OPTIONS = {"vacuum": "vacuum", "mop": "mop", "vacuum_and_mop": "vac_and_mop"}
_CLEANING_STATES = frozenset(
    {
        "cleaning",
        "spot_cleaning",
        "zoned_cleaning",
        "segment_cleaning",
        "robot_status_mopping",
        "clean_mop_cleaning",
        "clean_mop_mopping",
        "segment_mopping",
        "segment_clean_mop_cleaning",
        "segment_clean_mop_mopping",
        "zoned_mopping",
        "zoned_clean_mop_cleaning",
        "zoned_clean_mop_mopping",
    }
)
# `charger_disconnected` is how a resting robot off the dock reports after a
# while; see dev doc "Gerätezustand".
_IDLE_STATES = frozenset(
    {"idle", "charging", "charging_complete", "charger_disconnected"}
)
_DOCK_STATES = frozenset(
    {
        "charging",
        "charging_complete",
        "charging_problem",
        "washing_the_mop",
        "emptying_the_bin",
        "air_drying_stopping",
    }
)
_ERROR_STATES = frozenset({"error", "charging_problem", "device_offline", "locked"})


class RoborockAdapter(HomeAssistantVacuumAdapter):
    """Keep manufacturer mappings and protocol-specific commands behind the port."""

    _maps: dict[str, dict[str, Any]] | None = None

    def option_mapping(self, name: str) -> dict[str, str]:
        """Use published Roborock option names only where the entity offers them."""
        if self._configuration.get(name):
            return super().option_mapping(name)
        if name == "mode_options":
            configured = _MODE_OPTIONS
            entity_id = self.role_entity("cleaning_mode")
        elif name == "vacuum_levels":
            entity_id = self.entity_id
            configured = {
                "off": "off",
                "low": "quiet",
                "standard": "balanced",
                "high": "turbo",
                "maximum": "max",
                "maximum_plus": "max_plus",
            }
        elif name == "water_levels":
            entity_id = self.role_entity("mop_intensity")
            options = available_options(self._hass, entity_id)

            def first(*names: str) -> str:
                return next((item for item in names if item in options), names[0])

            configured = {
                "off": "off",
                "low": first("low", "mild"),
                "medium": first("medium", "moderate", "standard"),
                "high": first("high", "intense"),
            }
        else:
            entity_id = self.role_entity("mop_route")
            configured = {
                "fast": "fast",
                "standard": "standard",
                "deep": "deep",
                "deep_plus": "deep_plus",
            }
        return supported_mapping(configured, available_options(self._hass, entity_id))

    @property
    def native_segments(self) -> bool:
        """Require a recognized V1 profile plus the public command action."""
        return self._configuration.get("protocol") == "roborock_v1" and bool(
            self.supported_features & VacuumEntityFeature.SEND_COMMAND
        )

    @property
    def current_map_id(self) -> str | None:
        """Resolve only an unambiguous map name observed on the robot."""
        current = self.role_value("selected_map")
        maps = (
            {key: value["name"] for key, value in self._maps.items()}
            if self._maps is not None
            else self._configuration.get("map_options", {})
        )
        matches = [key for key, name in maps.items() if name == current]
        return matches[0] if len(matches) == 1 else None

    async def async_refresh_maps(self) -> None:
        """Read the public map inventory without accessing integration internals."""
        if not self.native_segments:
            return
        self._maps = None
        response = await self._hass.services.async_call(
            "roborock",
            "get_maps",
            {"entity_id": self.entity_id},
            blocking=True,
            context=physical_context(),
            return_response=True,
        )
        if not isinstance(response, dict):
            raise ConflictError("map_inventory_unavailable")
        payload = response.get(self.entity_id or "", response)
        maps = payload.get("maps") if isinstance(payload, dict) else None
        if not isinstance(maps, list):
            raise ConflictError("map_inventory_unavailable")
        parsed: dict[str, dict[str, Any]] = {}
        for item in maps:
            if (
                not isinstance(item, dict)
                or (
                    item.get("name") is not None
                    and not isinstance(item.get("name"), str)
                )
                or not isinstance(item.get("rooms"), dict)
                or isinstance(item.get("flag"), bool)
                or not isinstance(item.get("flag"), int)
            ):
                raise ConflictError("invalid_map_inventory")
            map_id = str(item["flag"])
            if map_id in parsed:
                raise ConflictError("ambiguous_map_inventory")
            rooms = item["rooms"]
            assert isinstance(rooms, dict)
            parsed[map_id] = {
                "name": item.get("name") or f"Map {map_id}",
                "rooms": {str(key): value for key, value in rooms.items()},
            }
        self._maps = parsed

    async def async_prepare(
        self, unit: WorkUnit, assignment: DispatchAssignment
    ) -> None:
        """Reject inventory changes before any settings or cleaning command."""
        try:
            await self.async_refresh_maps()
        except Exception as err:
            code = err.code if isinstance(err, OrchestratorError) else "map_read_failed"
            raise DispatchNotStartedError(code) from err
        await super().async_prepare(unit, assignment)

    def _validate_start(self, unit: WorkUnit, assignment: DispatchAssignment) -> None:
        # Never discover a different scope after the assignment was accepted.
        if self.native_segments and (self._maps is None or self.current_map_id is None):
            raise ConflictError("map_inventory_unavailable")
        super()._validate_start(unit, assignment)

    def _area_reach(
        self, room_id: str, segments: tuple[str, ...], area_id: str
    ) -> RoomReach:
        """Send the area's segments on the current map; others are ignored."""
        if not self.native_segments:
            return self._public_reach(super()._area_reach(room_id, segments, area_id))
        current = self.current_map_id
        if current is None:
            return RoomReach(room_id, ReachStatus.MAP_UNKNOWN)
        return self._segment_reach(room_id, segments, current)

    def _bound_reach(
        self,
        room_id: str,
        bindings: tuple[RoomBinding, ...],
        areas: Mapping[str, tuple[str, ...]],
    ) -> RoomReach:
        """Use the binding of the current map; segment IDs carry its flag."""
        if not self.native_segments:
            return self._public_reach(super()._bound_reach(room_id, bindings, areas))
        current = self.current_map_id
        if current is None:
            return RoomReach(room_id, ReachStatus.MAP_UNKNOWN)
        binding = next((item for item in bindings if item.map_id == current), None)
        if binding is None:
            return RoomReach(room_id, ReachStatus.NOT_ON_CURRENT_MAP)
        segments = tuple(f"{current}_{target}" for target in binding.target_ids)
        invalid = tuple(
            item for item in segments if not self._valid_segment(item, current)
        )
        if invalid:
            return RoomReach(room_id, ReachStatus.BINDING_INVALID, ignored=invalid)
        return RoomReach(room_id, ReachStatus.REACHABLE, segments, (), segments)

    def _segment_reach(
        self, room_id: str, segments: tuple[str, ...], current: str
    ) -> RoomReach:
        valid = tuple(item for item in segments if self._valid_segment(item, current))
        ignored = tuple(item for item in segments if item not in valid)
        if not valid:
            return RoomReach(room_id, ReachStatus.NOT_ON_CURRENT_MAP, ignored=ignored)
        return RoomReach(room_id, ReachStatus.REACHABLE, valid, ignored, valid)

    def _public_reach(self, reach: RoomReach) -> RoomReach:
        """HA sends every mapped segment; any unsafe one blocks the room."""
        unsafe = tuple(
            item for item in reach.physical if not self._safe_public_target(item)
        )
        if reach.status is not ReachStatus.REACHABLE or not unsafe:
            return reach
        return RoomReach(reach.room_id, ReachStatus.NOT_ON_CURRENT_MAP, ignored=unsafe)

    def _valid_segment(self, target: str, current: str) -> bool:
        parts = target.split("_")
        return (
            len(parts) == 2
            and parts[0] == current
            and parts[1].isascii()
            and parts[1].isdigit()
            and int(parts[1]) > 0
            and (self._maps is None or parts[1] in self._maps[current]["rooms"])
        )

    def _safe_public_target(self, target: str) -> bool:
        if "_" not in target:
            return target.isascii() and target.isdigit() and int(target) > 0
        current = self.current_map_id
        return current is not None and self._valid_segment(target, current)

    @property
    def profile(self) -> RobotProfile:
        """Advertise native repeats only for recognized V1 segment execution."""
        profile = super().profile
        if not self.native_segments:
            return profile
        revision = sha256(
            (
                profile.capabilities.revision
                + json.dumps(
                    {"map": self.current_map_id, "inventory": self._maps},
                    sort_keys=True,
                )
            ).encode()
        ).hexdigest()
        return replace(
            profile,
            capabilities=replace(
                profile.capabilities,
                revision=revision,
                area_addressing=AreaAddressing.VENDOR_SEGMENT,
                map_context=self.current_map_id,
                passes=PassCapability(3, PassScope.TARGET_SET),
            ),
        )

    async def _send_clean(self, unit: WorkUnit, assignment: DispatchAssignment) -> None:
        if not self.native_segments:
            await super()._send_clean(unit, assignment)
            return
        segments = list(
            dict.fromkeys(
                int(target.split("_")[1]) for target in assignment.adapter_targets
            )
        )
        report_adapter(TelemetryEvent.PHYSICAL, "requested")
        await self._hass.services.async_call(
            "vacuum",
            "send_command",
            {
                "entity_id": self.entity_id,
                "command": "app_segment_clean",
                "params": [{"segments": segments, "repeat": unit.passes}],
            },
            blocking=True,
            context=physical_context(),
        )

    async def async_observe(self) -> RobotObservation:
        """Separate floor cleaning from mapping, mop washing and dock activity."""
        observation = await super().async_observe()
        if observation.state is RobotAvailabilityState.UNKNOWN:
            return observation
        flag = self.role_value("in_cleaning")
        # Bound but unusable roles fail closed; see internal dev doc "Adapter".
        continuing = flag == "on" or (flag is None and self.role_bound("in_cleaning"))
        status = self.role_value("status")
        if status is None:
            if self.role_bound("status"):
                return replace(
                    observation,
                    state=RobotAvailabilityState.UNAVAILABLE
                    if observation.error_code
                    else RobotAvailabilityState.BUSY,
                    cleaning_active=None,
                    normal_end=False,
                    reason="status_unusable",
                )
            if not continuing:
                return observation
            return replace(
                observation,
                state=RobotAvailabilityState.UNAVAILABLE
                if observation.error_code
                else RobotAvailabilityState.BUSY,
                normal_end=False,
                reason="cleaning_continues",
            )
        error = observation.error_code or (status if status in _ERROR_STATES else None)
        idle = status in _IDLE_STATES and not continuing and not error
        return replace(
            observation,
            state=RobotAvailabilityState.UNAVAILABLE
            if error
            else RobotAvailabilityState.AVAILABLE
            if idle
            else RobotAvailabilityState.BUSY,
            cleaning_active=status in _CLEANING_STATES,
            normal_end=idle,
            error_code=error,
            reason=status,
            at_dock=status in _DOCK_STATES,
        )
