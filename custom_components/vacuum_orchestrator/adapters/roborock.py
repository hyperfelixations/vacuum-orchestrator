"""Roborock public-entity semantics and map-scoped V1 segment commands."""

import json
from dataclasses import replace
from hashlib import sha256
from typing import Any

from homeassistant.components.vacuum.const import VacuumEntityFeature

from ..domain.capabilities import AreaAddressing, PassCapability, RobotProfile
from ..domain.dispatching import RobotObservation
from ..domain.errors import ConflictError, DispatchNotStartedError, OrchestratorError
from ..domain.planning import DispatchAssignment, WorkUnit
from ..domain.types import PassScope, RobotAvailabilityState
from ..ha_context import physical_context
from ..ports.command_scope import check_command_authorization
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
_IDLE_STATES = frozenset({"idle", "charging", "charging_complete"})
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
            options = available_options(self._hass, entity_id)
            configured = {
                "low": "quiet",
                "standard": "balanced",
                "medium": "balanced",
                "high": "turbo",
                "maximum": "max_plus" if "max_plus" in options else "max",
            }
        elif name == "water_levels":
            entity_id = self.role_entity("mop_intensity")
            options = available_options(self._hass, entity_id)
            configured = {
                "low": "mild" if "mild" in options else "low",
                "standard": "standard" if "standard" in options else "medium",
                "medium": "standard" if "standard" in options else "medium",
                "high": "intense" if "intense" in options else "high",
            }
        else:
            entity_id = self.role_entity("mop_route")
            configured = {"standard": "standard", "deep": "deep", "fast": "fast"}
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

    async def async_dispatch(
        self, unit: WorkUnit, assignment: DispatchAssignment
    ) -> None:
        """Reject inventory changes before any settings or cleaning command."""
        try:
            await self.async_refresh_maps()
        except Exception as err:
            code = err.code if isinstance(err, OrchestratorError) else "map_read_failed"
            raise DispatchNotStartedError(code) from err
        await super().async_dispatch(unit, assignment)

    def target_mapping(self) -> dict[str, str | tuple[str, ...]]:
        """Exclude targets on other maps before the selector can assign a job."""
        if not self.native_segments:
            areas = self.area_mapping()
            return {
                room_id: targets
                for room_id, targets in super().target_mapping().items()
                if all(
                    self._safe_public_target(segment)
                    for area in ((targets,) if isinstance(targets, str) else targets)
                    for segment in areas[area]
                )
            }
        current = self.current_map_id
        if current is None:
            return {}
        areas = self.area_mapping()
        candidates: dict[str, tuple[str, ...]] = {}
        if self._rooms is None:
            candidates = {
                area: areas[area] for area in self._target_areas if area in areas
            }
        else:
            for room in self._rooms().values():
                explicit = any(
                    item.robot_id == self._robot_id for item in room.bindings
                )
                binding = next(
                    (
                        item
                        for item in room.bindings
                        if item.robot_id == self._robot_id and item.map_id == current
                    ),
                    None,
                )
                if binding is not None:
                    candidates[room.room_id] = tuple(
                        f"{current}_{target}" for target in binding.target_ids
                    )
                elif (
                    not explicit
                    and room.area_id in self._target_areas
                    and room.area_id in areas
                ):
                    candidates[room.room_id] = areas[room.area_id]
        return self._without_overlapping_targets(
            {
                room_id: targets
                for room_id, targets in candidates.items()
                if all(self._valid_segment(target, current) for target in targets)
            }
        )

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
        # A map inventory refresh is required before planning; never discover a
        # different scope after the immutable assignment has been accepted.
        if self._maps is None or self.current_map_id is None:
            raise ConflictError("map_inventory_unavailable")
        self._validate_dispatch(unit, assignment)
        check_command_authorization()
        segments = list(
            dict.fromkeys(
                int(target.split("_")[1]) for target in assignment.adapter_targets
            )
        )
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
        status = self.role_value("status")
        if status is None or observation.state is RobotAvailabilityState.UNKNOWN:
            return observation
        continuing = self.role_value("in_cleaning") == "on"
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
        )
