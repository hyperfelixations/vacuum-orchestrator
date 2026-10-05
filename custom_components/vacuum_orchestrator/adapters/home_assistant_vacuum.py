"""Conservative adapter using only public Home Assistant vacuum actions."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from contextlib import suppress
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

from homeassistant.components.vacuum.const import VacuumEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from ..configuration_values import requirement_from_data
from ..domain.capabilities import (
    AreaAddressing,
    CancelSemantics,
    CompletionEvidence,
    PassCapability,
    RobotCapabilities,
    RobotProfile,
    StartEvidence,
)
from ..domain.dispatching import (
    RobotObservation,
    assignment_supports_current_capabilities,
)
from ..domain.errors import (
    ConflictError,
    DispatchNotStartedError,
    OrchestratorError,
    StaleCommandError,
    ValidationError,
)
from ..domain.execution import ExecutionPolicy
from ..domain.planning import DispatchAssignment, WorkUnit
from ..domain.rooms import Room
from ..domain.types import (
    MopRoute,
    OperationKind,
    PassScope,
    RobotAvailabilityState,
    SemanticLevel,
)
from ..ha_context import physical_context
from ..ports.command_scope import check_command_authorization
from ..ports.telemetry import TelemetryEvent, report_adapter
from .discovery import candidate_for, resolve_entity_id
from .settings import async_set_option, available_options, supported_mapping


class HomeAssistantVacuumAdapter:
    """Bind stable entities and prove exact semantics before every cleaning start."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        robot_id: str,
        source_robot_id: str,
        entity_id: str,
        adapter_name: str = "home_assistant",
        target_areas: tuple[str, ...] = (),
        last_clean_start_entity_id: str | None = None,
        last_clean_end_entity_id: str | None = None,
        configuration: Mapping[str, Any] | None = None,
        rooms: Callable[[], Mapping[str, Room]] | None = None,
    ) -> None:
        self._hass = hass
        self._robot_id = robot_id
        self._source_robot_id = source_robot_id
        self._adapter_name = adapter_name
        self._configuration = dict(configuration or {})
        self._fallback_entity_id = entity_id
        self._target_areas = target_areas
        self._rooms = rooms
        self._roles: dict[str, str | None] = {}
        entity = er.async_get(hass).async_get(entity_id)
        self._registry_id = self._configuration.get("robot_registry_id") or (
            entity.id if entity else None
        )
        if self._registry_id:
            with suppress(ValidationError):
                self._roles.update(candidate_for(hass, self._registry_id).roles)
        self._roles.update(self._configuration.get("roles", {}))
        self._legacy_roles = {
            "last_clean_start": last_clean_start_entity_id,
            "last_clean_end": last_clean_end_entity_id,
        }

    @property
    def entity_id(self) -> str | None:
        """Follow registry renames and fail closed on deletion or disablement."""
        if self._registry_id is not None:
            return resolve_entity_id(self._hass, self._registry_id)
        return self._fallback_entity_id

    def role_entity(self, role: str) -> str | None:
        """Resolve a discovered or explicitly overridden companion binding."""
        if role in self._roles:
            reference = self._roles[role]
            return (
                None if reference is None else resolve_entity_id(self._hass, reference)
            )
        return self._legacy_roles.get(role)

    def role_bound(self, role: str) -> bool:
        """Return whether a binding exists, even if its entity is unusable now."""
        return (
            self._roles.get(role) is not None
            or self._legacy_roles.get(role) is not None
        )

    def role_value(self, role: str) -> str | None:
        """Return only usable companion states."""
        entity_id = self.role_entity(role)
        state = self._hass.states.get(entity_id) if entity_id else None
        return (
            None
            if state is None or state.state in {"unknown", "unavailable"}
            else state.state
        )

    @property
    def supported_features(self) -> VacuumEntityFeature:
        """Read live supported actions without inferring cleaning semantics."""
        entity_id = self.entity_id
        state = self._hass.states.get(entity_id) if entity_id else None
        value = state.attributes.get("supported_features", 0) if state else 0
        return VacuumEntityFeature(value if isinstance(value, int) else 0)

    def option_mapping(self, name: str) -> dict[str, str]:
        """Resolve explicit semantic mappings against live entity options."""
        entity_id = (
            self.entity_id
            if name == "vacuum_levels"
            else self.role_entity(
                {
                    "mode_options": "cleaning_mode",
                    "water_levels": "mop_intensity",
                    "mop_routes": "mop_route",
                }[name]
            )
        )
        return supported_mapping(
            self._configuration.get(name, {}), available_options(self._hass, entity_id)
        )

    def area_mapping(self) -> dict[str, tuple[str, ...]]:
        """Read the public HA area mapping; incomplete mappings are never sent."""
        entry = er.async_get(self._hass).async_get(
            self._registry_id or self.entity_id or ""
        )
        raw = (
            dict(entry.options.get("vacuum") or {}).get("area_mapping", {})
            if entry
            else {}
        )
        if not isinstance(raw, dict):
            return {}
        return {
            key: tuple(value)
            for key, value in raw.items()
            if isinstance(key, str)
            and isinstance(value, list)
            and value
            and all(isinstance(item, str) and item for item in value)
        }

    def target_mapping(self) -> dict[str, str | tuple[str, ...]]:
        """Map canonical rooms only to fully configured HA areas."""
        areas = self.area_mapping()
        allowed = set(self._target_areas)
        result: dict[str, str | tuple[str, ...]] = {}
        if self._rooms is None:
            result = {area: area for area in allowed if area in areas}
        for room in self._rooms().values() if self._rooms is not None else ():
            explicit = any(item.robot_id == self._robot_id for item in room.bindings)
            binding = next(
                (
                    item
                    for item in room.bindings
                    if item.robot_id == self._robot_id and item.map_id is None
                ),
                None,
            )
            if binding is not None:
                if binding.map_id is None and all(
                    target in areas for target in binding.target_ids
                ):
                    result[room.room_id] = binding.target_ids
            elif not explicit and room.area_id in allowed and room.area_id in areas:
                result[room.room_id] = room.area_id
        physical = {
            room_id: tuple(
                segment
                for area in ((targets,) if isinstance(targets, str) else targets)
                for segment in areas[area]
            )
            for room_id, targets in result.items()
        }
        unique = self._without_overlapping_targets(physical)
        return {
            room_id: targets for room_id, targets in result.items() if room_id in unique
        }

    @staticmethod
    def _without_overlapping_targets(
        targets: dict[str, tuple[str, ...]],
    ) -> dict[str, str | tuple[str, ...]]:
        """Reject room aliases that would share physical ownership or receipts."""
        owners: dict[str, set[str]] = {}
        for room_id, values in targets.items():
            for value in values:
                owners.setdefault(value, set()).add(room_id)
        return {
            room_id: values
            for room_id, values in targets.items()
            if all(len(owners[value]) == 1 for value in values)
        }

    @property
    def profile(self) -> RobotProfile:
        """Return a live capability snapshot independent of ordinary state changes."""
        modes = self.option_mapping("mode_options")
        fixed = self._configuration.get("fixed_mode")
        operations = {OperationKind(key) for key in modes}
        if fixed is not None:
            operations.add(OperationKind(fixed))
        if not self.supported_features & VacuumEntityFeature.CLEAN_AREA:
            operations.clear()
        targets = self.target_mapping()
        settings = {
            name: self.option_mapping(name)
            for name in ("vacuum_levels", "water_levels", "mop_routes")
        }
        revision_source = {
            "entity_id": self.entity_id,
            "features": int(self.supported_features),
            "targets": targets,
            "area_mapping": self.area_mapping(),
            "roles": {role: self.role_entity(role) for role in self._roles},
            "modes": modes,
            "fixed": fixed,
            "settings": settings,
        }
        capabilities = RobotCapabilities(
            revision=sha256(
                json.dumps(revision_source, sort_keys=True).encode()
            ).hexdigest(),
            operations=frozenset(operations),
            area_addressing=AreaAddressing.HOME_ASSISTANT_AREA,
            target_map=targets,
            map_context=None,
            passes=PassCapability(1, PassScope.TARGET_SET),
            vacuum_levels=frozenset(
                SemanticLevel(key) for key in settings["vacuum_levels"]
            ),
            water_levels=frozenset(
                SemanticLevel(key) for key in settings["water_levels"]
            ),
            mop_routes=frozenset(MopRoute(key) for key in settings["mop_routes"]),
            cancel=CancelSemantics.STOP
            if self.supported_features & VacuumEntityFeature.STOP
            else CancelSemantics.UNSUPPORTED,
            start_evidence=frozenset({StartEvidence.ACTIVITY_START_TRANSITION}),
            completion_evidence=frozenset(
                {CompletionEvidence.ACTIVITY_TERMINAL_TRANSITION}
            ),
        )
        return RobotProfile(
            self._robot_id,
            self._source_robot_id,
            self._adapter_name,
            capabilities,
            allowed_operations=frozenset(
                OperationKind(value)
                for value in self._configuration["allowed_operations"]
            )
            if "allowed_operations" in self._configuration
            else None,
            preference=self._configuration.get("preference", 0),
            minimum_battery=self._configuration.get("minimum_battery"),
            requirements=tuple(
                requirement_from_data(value)
                for value in self._configuration.get("requirements", [])
            ),
            execution_policy=ExecutionPolicy(
                self._configuration.get("start_timeout_seconds", 180),
                self._configuration.get("run_timeout_seconds", 14400),
                self._configuration.get("cancel_timeout_seconds", 120),
                self._configuration.get("settle_seconds", 30),
            ),
        )

    @property
    def watched_entity_ids(self) -> tuple[str, ...]:
        """Return current bindings for availability, settings and run evidence."""
        return tuple(
            dict.fromkeys(
                item
                for item in (
                    self.entity_id,
                    *(
                        self.role_entity(role)
                        for role in dict.fromkeys((*self._roles, *self._legacy_roles))
                    ),
                )
                if item is not None
            )
        )

    def observed_operation(self) -> OperationKind | None:
        """Decode an observed mode without interpreting custom or smart as intent."""
        value = self.role_value("cleaning_mode")
        matches = [
            OperationKind(key)
            for key, option in self.option_mapping("mode_options").items()
            if option == value
        ]
        return matches[0] if len(matches) == 1 else None

    async def async_observe(self) -> RobotObservation:
        """Separate cleaning activity, availability and plausible normal endings."""
        entity_id = self.entity_id
        state = self._hass.states.get(entity_id) if entity_id else None
        usable = state is not None and state.state not in {"unknown", "unavailable"}
        battery = None
        if usable and state is not None:
            raw = self.role_value("battery") or state.attributes.get("battery_level")
            with suppress(ValueError, TypeError):
                battery = (
                    int(raw) if raw is not None and not isinstance(raw, bool) else None
                )
            if battery is not None and not 0 <= battery <= 100:
                battery = None
        error = self.role_value("error")
        error = None if error in {None, "none", "0", "no_error", "ok"} else error
        idle = usable and state is not None and state.state in {"idle", "docked"}
        if state is not None and state.state == "error":
            error = error or "device_error"
        return RobotObservation(
            self._robot_id,
            self._source_robot_id,
            RobotAvailabilityState.UNKNOWN
            if not usable
            else RobotAvailabilityState.UNAVAILABLE
            if error
            else RobotAvailabilityState.AVAILABLE
            if idle
            else RobotAvailabilityState.BUSY,
            battery,
            state.state if state else "entity_missing",
            datetime.now(UTC),
            self._history_value("last_clean_start"),
            self._history_value("last_clean_end"),
            cleaning_active=(state.state == "cleaning")
            if usable and state is not None
            else None,
            normal_end=bool(idle and not error),
            error_code=error,
            observed_operation=self.observed_operation(),
        )

    def _history_value(self, role: str) -> datetime | None:
        value = self.role_value(role)
        parsed = dt_util.parse_datetime(value) if value else None
        return (
            parsed.astimezone(UTC)
            if parsed is not None and parsed.tzinfo is not None
            else None
        )

    def _validate_dispatch(
        self, unit: WorkUnit, assignment: DispatchAssignment
    ) -> None:
        if assignment.work_unit_id != unit.work_unit_id:
            raise ConflictError("assignment_work_unit_mismatch")
        if not assignment.adapter_targets:
            raise ConflictError("empty_adapter_targets")
        profile = self.profile
        if not assignment_supports_current_capabilities(assignment, profile):
            raise ConflictError("capabilities_changed")
        if unit.operation not in profile.effective_operations:
            raise ConflictError("unsupported_operation")
        expected = tuple(
            part
            for target in unit.canonical_targets
            for part in profile.capabilities.targets_for(target)
        )
        if expected != assignment.adapter_targets or any(
            target not in profile.capabilities.target_map
            for target in unit.canonical_targets
        ):
            raise ConflictError("unmapped_target")
        if (
            unit.passes > profile.capabilities.passes.maximum
            or unit.pass_scope != profile.capabilities.passes.scope
        ):
            raise ConflictError("unsupported_pass_count")
        if unit.vendor_extension is not None:
            raise ConflictError("unsupported_vendor_extension")

    async def async_dispatch(
        self, unit: WorkUnit, assignment: DispatchAssignment
    ) -> None:
        """Apply confirmed settings, revalidate all targets, then start once."""
        try:
            self._validate_dispatch(unit, assignment)
            observation = await self.async_observe()
            if observation.state is not RobotAvailabilityState.AVAILABLE:
                raise ConflictError("robot_not_available")
            await self._apply_settings(unit, assignment)
            self._validate_dispatch(unit, assignment)
            observation = await self.async_observe()
            if observation.state is not RobotAvailabilityState.AVAILABLE:
                raise ConflictError("robot_not_available")
            check_command_authorization()
        except StaleCommandError:
            raise
        except Exception as err:
            code = err.code if isinstance(err, OrchestratorError) else "settings_failed"
            raise DispatchNotStartedError(code) from err
        await self._send_clean(unit, assignment)

    async def _apply_settings(
        self, unit: WorkUnit, assignment: DispatchAssignment
    ) -> None:
        timeout = float(self._configuration.get("settings_timeout_seconds", 45))
        mode_entity = self.role_entity("cleaning_mode")
        mode = self.option_mapping("mode_options").get(unit.operation.value)
        if mode is not None and mode_entity is not None:
            await self._ensure_available()
            await async_set_option(
                self._hass,
                mode_entity,
                mode,
                confirmation_seconds=timeout,
                setting="cleaning_mode",
            )
        elif self._configuration.get("fixed_mode") != unit.operation.value:
            raise ConflictError("unsupported_operation")
        for key, setting, value, entity_id in (
            (
                "vacuum_power",
                "vacuum_levels",
                unit.preferences.vacuum_power,
                self.entity_id,
            ),
            (
                "mop_intensity",
                "water_levels",
                unit.preferences.mop_intensity,
                self.role_entity("mop_intensity"),
            ),
            (
                "mop_route",
                "mop_routes",
                unit.preferences.mop_route,
                self.role_entity("mop_route"),
            ),
        ):
            if key not in assignment.preference_resolution.applied:
                if key in assignment.preference_resolution.omitted:
                    report_adapter(TelemetryEvent.SETTING, "omitted", key)
                continue
            option = (
                self.option_mapping(setting).get(value.value)
                if value is not None
                else None
            )
            if option is None or entity_id is None:
                raise ConflictError("setting_option_unavailable")
            await self._ensure_available()
            await async_set_option(
                self._hass, entity_id, option, confirmation_seconds=timeout, setting=key
            )
        observed = self.observed_operation()
        if mode is not None and observed != unit.operation:
            raise ConflictError("cleaning_mode_not_confirmed")
        if observed is not None and observed != unit.operation:
            raise ConflictError("cleaning_mode_conflicts_with_fixed_mode")

    async def _ensure_available(self) -> None:
        if (await self.async_observe()).state is not RobotAvailabilityState.AVAILABLE:
            raise ConflictError("robot_not_available")

    async def _send_clean(self, unit: WorkUnit, assignment: DispatchAssignment) -> None:
        check_command_authorization()
        report_adapter(TelemetryEvent.PHYSICAL, "requested")
        await self._hass.services.async_call(
            "vacuum",
            "clean_area",
            {
                "entity_id": self.entity_id,
                "cleaning_area_id": list(dict.fromkeys(assignment.adapter_targets)),
            },
            blocking=True,
            context=physical_context(),
        )

    async def async_cancel(self) -> None:
        """Stop only when the current public entity advertises stop support."""
        if not self.supported_features & VacuumEntityFeature.STOP:
            raise ConflictError("unsupported_cancel_semantics")
        check_command_authorization()
        report_adapter(TelemetryEvent.PHYSICAL, "requested", "stopped")
        await self._hass.services.async_call(
            "vacuum",
            "stop",
            {"entity_id": self.entity_id},
            blocking=True,
            context=physical_context(),
        )
