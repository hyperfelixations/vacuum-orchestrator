"""Registry-based robot and companion-entity discovery without physical I/O."""

from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from types import MappingProxyType

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from ..domain.errors import ValidationError

_ROBOROCK_ROLES = {
    ("sensor", "battery"): "battery",
    ("sensor", "status"): "status",
    ("sensor", "vacuum_error"): "error",
    ("sensor", "dock_error"): "dock_error",
    ("sensor", "last_clean_start"): "last_clean_start",
    ("sensor", "last_clean_end"): "last_clean_end",
    ("sensor", "current_room"): "current_room",
    ("sensor", "clean_percent"): "clean_percent",
    ("select", "cleaning_mode"): "cleaning_mode",
    ("select", "mop_intensity"): "mop_intensity",
    ("select", "mop_mode"): "mop_route",
    ("select", "selected_map"): "selected_map",
    ("binary_sensor", "in_cleaning"): "in_cleaning",
    ("binary_sensor", "water_shortage"): "water_shortage",
    ("binary_sensor", "mop_attached"): "mop_attached",
    ("binary_sensor", "water_box_attached"): "water_box_attached",
    ("binary_sensor", "dirty_box_full"): "dirty_box_full",
    ("binary_sensor", "clean_box_empty"): "clean_box_empty",
    ("binary_sensor", "clean_fluid_empty"): "clean_fluid_empty",
}


@dataclass(frozen=True, slots=True)
class RobotCandidate:
    """An inspectable discovery result; ambiguous roles remain unassigned."""

    registry_id: str
    entity_id: str
    name: str
    adapter: str
    source_robot_id: str
    device_id: str | None
    roles: Mapping[str, str]
    ambiguous_roles: Mapping[str, tuple[str, ...]]
    area_targets: Mapping[str, tuple[str, ...]]
    protocol: str | None


def resolve_entity_id(hass: HomeAssistant, registry_id: str) -> str | None:
    """Resolve a stable registry binding on every use, including after rename."""
    entry = er.async_get(hass).async_get(registry_id)
    return None if entry is None or entry.disabled else entry.entity_id


def discover_robots(hass: HomeAssistant) -> tuple[RobotCandidate, ...]:
    """Inspect registered vacuums and versioned companion-entity roles."""
    registry = er.async_get(hass)
    devices = dr.async_get(hass)
    candidates = []
    for vacuum in registry.entities.values():
        if vacuum.domain != "vacuum" or vacuum.disabled:
            continue
        device = devices.async_get(vacuum.device_id) if vacuum.device_id else None
        companion_ids = {vacuum.device_id} if vacuum.device_id else set()
        source_id = (
            f"device_registry:{vacuum.device_id}"
            if vacuum.device_id
            else f"entity_registry:{vacuum.id}"
        )
        if device is not None and vacuum.platform == "roborock":
            identifiers = {
                value for domain, value in device.identifiers if domain == "roborock"
            }
            if len(identifiers) == 1:
                duid = next(iter(identifiers))
                source_id = f"roborock:{duid}"
                dock = (
                    devices.async_get_device_by_identifier(
                        ("roborock", f"{duid}_dock"), vacuum.config_entry_id
                    )
                    if vacuum.config_entry_id
                    else None
                )
                if dock is not None:
                    companion_ids.add(dock.id)
        if isinstance(device, dr.DeviceEntry):
            macs = {
                value.lower().replace(":", "").replace("-", "")
                for kind, value in device.connections
                if kind == dr.CONNECTION_NETWORK_MAC
            }
            if len(macs) == 1:
                source_id = "physical:" + sha256(next(iter(macs)).encode()).hexdigest()
        matches: dict[str, list[str]] = {}
        for entity in registry.entities.values():
            if (
                entity.disabled
                or entity.device_id not in companion_ids
                or entity.platform != vacuum.platform
            ):
                continue
            role = _role_for(entity, vacuum.platform)
            if role is not None:
                matches.setdefault(role, []).append(entity.id)
        roles = {
            role: values[0] for role, values in matches.items() if len(values) == 1
        }
        ambiguous = {
            role: tuple(sorted(values))
            for role, values in matches.items()
            if len(values) > 1
        }
        raw_mapping = dict(vacuum.options.get("vacuum") or {}).get("area_mapping", {})
        area_targets = {}
        if isinstance(raw_mapping, dict):
            for area_id, targets in raw_mapping.items():
                if (
                    isinstance(area_id, str)
                    and isinstance(targets, list)
                    and targets
                    and all(isinstance(item, str) and item for item in targets)
                ):
                    area_targets[area_id] = tuple(targets)
        protocol = (
            "roborock_v1"
            if vacuum.platform == "roborock"
            and {"selected_map", "mop_route"} <= roles.keys()
            else None
        )
        candidates.append(
            RobotCandidate(
                vacuum.id,
                vacuum.entity_id,
                vacuum.name or vacuum.original_name or vacuum.entity_id,
                "roborock" if vacuum.platform == "roborock" else "home_assistant",
                source_id,
                vacuum.device_id,
                MappingProxyType(roles),
                MappingProxyType(ambiguous),
                MappingProxyType(area_targets),
                protocol,
            )
        )
    return tuple(sorted(candidates, key=lambda item: item.registry_id))


def candidate_for(hass: HomeAssistant, entity_or_registry_id: str) -> RobotCandidate:
    """Resolve a configurable vacuum without accepting another entity domain."""
    entry = er.async_get(hass).async_get(entity_or_registry_id)
    if entry is None:
        raise ValidationError("entity_not_registered")
    return (
        next(
            (
                candidate
                for candidate in discover_robots(hass)
                if candidate.registry_id == entry.id
            ),
            None,
        )
        or _missing_vacuum()
    )


def _missing_vacuum() -> RobotCandidate:
    raise ValidationError("vacuum_unavailable")


def _role_for(entity: er.RegistryEntry, platform: str) -> str | None:
    if (
        entity.domain == "sensor"
        and (entity.device_class or entity.original_device_class) == "battery"
    ):
        return "battery"
    if platform == "roborock":
        return _ROBOROCK_ROLES.get((entity.domain, entity.translation_key or ""))
    if (
        platform == "matter"
        and entity.domain == "select"
        and entity.translation_key == "clean_mode"
    ):
        return "cleaning_mode"
    return None
