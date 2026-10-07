"""Canonical robot configuration validation for flows and public commands."""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .adapters.discovery import candidate_for
from .configuration_values import normalize_requirements
from .const import (
    CONF_ADAPTER,
    CONF_LAST_CLEAN_END_ENTITY_ID,
    CONF_LAST_CLEAN_START_ENTITY_ID,
    CONF_ROBOT_ENTITY_ID,
    CONF_ROBOT_REGISTRY_ID,
    CONF_TARGET_AREAS,
    SUBENTRY_TYPE_ROBOT,
)
from .domain.errors import ConflictError, ValidationError
from .domain.types import MopRoute, OperationKind, VacuumLevel, WaterLevel
from .domain.validation import identifier, seconds


def validate_robot_configuration(
    hass: HomeAssistant,
    entry: ConfigEntry,
    data: Mapping[str, Any],
    *,
    robot_id: str | None = None,
) -> dict[str, Any]:
    """Normalize stable bindings and reject duplicates or invented capabilities."""
    allowed_fields = {
        CONF_ROBOT_REGISTRY_ID,
        CONF_ROBOT_ENTITY_ID,
        CONF_ADAPTER,
        CONF_LAST_CLEAN_START_ENTITY_ID,
        CONF_LAST_CLEAN_END_ENTITY_ID,
        CONF_TARGET_AREAS,
        "source_robot_id",
        "roles",
        "requirements",
        "allowed_operations",
        "enabled",
        "protocol",
        "fixed_mode",
        "preference",
        "minimum_battery",
        "mode_options",
        "vacuum_levels",
        "water_levels",
        "mop_routes",
        "map_options",
        "start_timeout_seconds",
        "run_timeout_seconds",
        "cancel_timeout_seconds",
        "settle_seconds",
        "settings_timeout_seconds",
        "return_timeout_seconds",
        "physical_robot_id",
    }
    if set(data) - allowed_fields:
        raise ValidationError("unknown_robot_configuration_field")
    if robot_id is None and len(entry.subentries) >= 20:
        raise ValidationError("robot_limit_reached")
    if robot_id is not None:
        require_idle_robot(entry, robot_id)
    candidate = candidate_for(
        hass,
        str(data.get(CONF_ROBOT_REGISTRY_ID) or data.get(CONF_ROBOT_ENTITY_ID, "")),
    )
    if robot_id is not None and (
        entry.subentries[robot_id].data[CONF_ROBOT_REGISTRY_ID] != candidate.registry_id
    ):
        raise ValidationError("robot_identity_change")
    for subentry_id, subentry in entry.subentries.items():
        if subentry_id == robot_id:
            continue
        if (
            subentry.data.get(CONF_ROBOT_REGISTRY_ID) == candidate.registry_id
            or subentry.data.get("source_robot_id") == candidate.source_robot_id
        ):
            raise ConflictError("already_configured")
    result = dict(data)
    result.update(
        {
            CONF_ROBOT_REGISTRY_ID: candidate.registry_id,
            CONF_ROBOT_ENTITY_ID: candidate.entity_id,
            CONF_ADAPTER: candidate.adapter,
            "source_robot_id": candidate.source_robot_id,
        }
    )
    raw_roles = data.get("roles", {})
    if not isinstance(raw_roles, dict):
        raise ValidationError("invalid_role_mapping")
    manual_roles = dict(raw_roles)
    for old_key, role in (
        (CONF_LAST_CLEAN_START_ENTITY_ID, "last_clean_start"),
        (CONF_LAST_CLEAN_END_ENTITY_ID, "last_clean_end"),
    ):
        if data.get(old_key):
            manual_roles[role] = data[old_key]
    allowed_domains = {
        "battery": "sensor",
        "status": "sensor",
        "error": "sensor",
        "dock_error": "sensor",
        "last_clean_start": "sensor",
        "last_clean_end": "sensor",
        "current_room": "sensor",
        "clean_percent": "sensor",
        "cleaning_mode": "select",
        "mop_intensity": "select",
        "mop_route": "select",
        "selected_map": "select",
        "in_cleaning": "binary_sensor",
        "water_shortage": "binary_sensor",
        "mop_attached": "binary_sensor",
        "water_box_attached": "binary_sensor",
        "dirty_box_full": "binary_sensor",
        "clean_box_empty": "binary_sensor",
        "clean_fluid_empty": "binary_sensor",
    }
    roles: dict[str, str | None] = {}
    for role, reference in manual_roles.items():
        if role not in allowed_domains:
            raise ValidationError("unknown_entity_role", role)
        if reference is None:
            roles[role] = None
            continue
        entity = er.async_get(hass).async_get(str(reference))
        if entity is None or entity.domain != allowed_domains[role]:
            raise ValidationError("invalid_role_entity", role)
        roles[role] = entity.id
    result["roles"] = roles
    result["requirements"] = normalize_requirements(hass, data.get("requirements", []))
    # No restriction (None or empty) follows the HA area mapping live.
    targets = data.get(CONF_TARGET_AREAS) or None
    if targets is not None and not isinstance(targets, list):
        raise ValidationError("invalid_target_areas")
    for target in targets or ():
        identifier(target, "invalid_target_areas")
    result[CONF_TARGET_AREAS] = (
        None if targets is None else list(dict.fromkeys(targets))
    )
    operations = data.get("allowed_operations", [item.value for item in OperationKind])
    if not isinstance(operations, list):
        raise ValidationError("invalid_operation")
    if not operations:
        raise ValidationError("empty_allowed_operations")
    try:
        result["allowed_operations"] = [
            OperationKind(value).value for value in operations
        ]
    except ValueError as err:
        raise ValidationError("invalid_operation") from err
    enabled = data.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValidationError("invalid_robot_enabled")
    result["enabled"] = enabled
    protocol = data.get("protocol", candidate.protocol)
    if protocol not in (None, "roborock_v1") or (
        protocol is not None and candidate.adapter != "roborock"
    ):
        raise ValidationError("unsupported_robot_protocol")
    result["protocol"] = protocol
    fixed_mode = data.get("fixed_mode")
    try:
        result["fixed_mode"] = (
            None if fixed_mode is None else OperationKind(fixed_mode).value
        )
    except ValueError as err:
        raise ValidationError("invalid_operation") from err
    preference = data.get("preference", 0)
    if (
        isinstance(preference, bool)
        or not isinstance(preference, int)
        or not -100 <= preference <= 100
    ):
        raise ValidationError("invalid_robot_preference")
    result["preference"] = preference
    minimum = data.get("minimum_battery")
    if minimum is not None and (
        isinstance(minimum, bool)
        or not isinstance(minimum, int)
        or not 0 <= minimum <= 100
    ):
        raise ValidationError("invalid_minimum_battery")
    result["minimum_battery"] = minimum
    for name in (
        "mode_options",
        "vacuum_levels",
        "water_levels",
        "mop_routes",
        "map_options",
    ):
        options = data.get(name, {})
        if not isinstance(options, dict) or any(
            not isinstance(key, str) or not isinstance(value, str) or not value
            for key, value in options.items()
        ):
            raise ValidationError("invalid_option_mapping", name)
        result[name] = dict(options)
        valid_keys = {
            "mode_options": {item.value for item in OperationKind},
            "vacuum_levels": {item.value for item in VacuumLevel},
            "water_levels": {item.value for item in WaterLevel},
            "mop_routes": {item.value for item in MopRoute},
        }.get(name)
        if valid_keys is not None and not set(options) <= valid_keys:
            raise ValidationError("invalid_option_mapping", name)
        if name == "mode_options" and len(set(options.values())) != len(options):
            raise ValidationError("ambiguous_mode_mapping")
    for field_name, default in {
        "start_timeout_seconds": 180,
        "run_timeout_seconds": 14400,
        "cancel_timeout_seconds": 120,
        "settle_seconds": 30,
        "settings_timeout_seconds": 45,
        "return_timeout_seconds": 900,
    }.items():
        value = data.get(field_name, default)
        if not isinstance(value, (int, float)):
            raise ValidationError("invalid_duration")
        seconds(value, positive=True)
        if value > 86400:
            raise ValidationError("timeout_out_of_range")
        result[field_name] = float(value)
    physical_id = data.get("physical_robot_id")
    if physical_id is not None:
        identifier(physical_id)
        result["source_robot_id"] = f"configured:{physical_id}"
    for subentry_id, subentry in entry.subentries.items():
        if (
            subentry_id != robot_id
            and subentry.data.get("source_robot_id") == result["source_robot_id"]
        ):
            raise ConflictError("already_configured")
    return result


# Minor-0 mapping keys; `None` drops a key that has no rung any more.
_MINOR_ZERO_KEYS: dict[str, dict[str, str | None]] = {
    "vacuum_levels": {"medium": "standard", "auto": None},
    "water_levels": {"standard": "medium", "maximum": "high", "auto": None},
    "mop_routes": {"auto": None},
}


def migrate_setting_mappings(data: Mapping[str, Any]) -> dict[str, Any]:
    """Move minor-0 option mapping keys onto the ordered ladders."""
    result = dict(data)
    for name, renamed in _MINOR_ZERO_KEYS.items():
        options = data.get(name)
        if not isinstance(options, Mapping):
            continue
        migrated = {key: value for key, value in options.items() if key not in renamed}
        for key, target in renamed.items():
            if target is not None and key in options:
                migrated.setdefault(target, options[key])
        result[name] = migrated
    return result


def require_idle_robot(entry: ConfigEntry, robot_id: str) -> ConfigSubentry:
    """Validate mutation ownership for both configuration flows and public commands."""
    subentry = entry.subentries.get(robot_id)
    if subentry is None or subentry.subentry_type != SUBENTRY_TYPE_ROBOT:
        raise ConflictError("unknown_robot")
    runtime = getattr(entry, "runtime_data", None)
    if runtime is not None and any(
        lease.robot_id == robot_id
        for lease in runtime.orchestrator.state.robot_leases.values()
    ):
        raise ConflictError("robot_busy")
    return subentry


def configure_robot(
    hass: HomeAssistant,
    entry: ConfigEntry,
    data: Mapping[str, Any],
    *,
    robot_id: str | None = None,
) -> str:
    """Apply one validated idle robot mutation without reloading the runtime."""
    previous = require_idle_robot(entry, robot_id) if robot_id else None
    merged = {**(previous.data if previous else {}), **data}
    if CONF_ROBOT_ENTITY_ID in data:
        merged.pop(CONF_ROBOT_REGISTRY_ID, None)
    normalized = validate_robot_configuration(hass, entry, merged, robot_id=robot_id)
    if previous is not None:
        hass.config_entries.async_update_subentry(entry, previous, data=normalized)
        return previous.subentry_id
    candidate = candidate_for(hass, normalized[CONF_ROBOT_REGISTRY_ID])
    subentry = ConfigSubentry(
        data=MappingProxyType(normalized),
        subentry_type=SUBENTRY_TYPE_ROBOT,
        title=candidate.name,
        unique_id=candidate.registry_id,
    )
    hass.config_entries.async_add_subentry(entry, subentry)
    return subentry.subentry_id
