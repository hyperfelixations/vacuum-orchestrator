"""Live robot composition from subentries and public HA registries."""

from collections.abc import Callable, Mapping
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .adapters.discovery import candidate_for
from .adapters.home_assistant_vacuum import HomeAssistantVacuumAdapter
from .adapters.roborock import RoborockAdapter
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
from .domain.rooms import Room
from .ports.robot import RobotAdapter


def build_adapters(
    hass: HomeAssistant, entry: ConfigEntry, rooms: Callable[[], Mapping[str, Room]]
) -> tuple[dict[str, RobotAdapter], dict[str, frozenset[str]]]:
    """Retain stored physical identities and additionally claim detected aliases."""
    adapters: dict[str, RobotAdapter] = {}
    claims: dict[str, frozenset[str]] = {}
    identities: set[str] = set()
    profiles = [
        item
        for item in entry.subentries.values()
        if item.subentry_type == SUBENTRY_TYPE_ROBOT
    ]
    if len(profiles) > 20:
        raise ConflictError("robot_limit_reached")
    for subentry in profiles:
        data: dict[str, Any] = dict(subentry.data)
        registry_id = str(data[CONF_ROBOT_REGISTRY_ID])
        source_id = str(data.get("source_robot_id", f"entity_registry:{registry_id}"))
        aliases = {source_id, f"entity_registry:{registry_id}"}
        try:
            candidate = candidate_for(hass, registry_id)
            aliases.add(candidate.source_robot_id)
            data.setdefault("protocol", candidate.protocol)
        except ValidationError:
            pass
        if identities & aliases:
            raise ConflictError("duplicate_physical_robot")
        identities.update(aliases)
        if not data.get("enabled", True):
            continue
        claims[subentry.subentry_id] = frozenset(aliases)
        adapter_type = (
            RoborockAdapter
            if data.get(CONF_ADAPTER) == "roborock"
            else HomeAssistantVacuumAdapter
        )
        adapters[subentry.subentry_id] = adapter_type(
            hass,
            robot_id=subentry.subentry_id,
            source_robot_id=source_id,
            entity_id=str(data[CONF_ROBOT_ENTITY_ID]),
            adapter_name=str(data.get(CONF_ADAPTER, "home_assistant")),
            target_areas=None
            if data.get(CONF_TARGET_AREAS) is None
            else tuple(str(item) for item in data[CONF_TARGET_AREAS]),
            last_clean_start_entity_id=data.get(CONF_LAST_CLEAN_START_ENTITY_ID),
            last_clean_end_entity_id=data.get(CONF_LAST_CLEAN_END_ENTITY_ID),
            configuration=data,
            rooms=rooms,
        )
    return adapters, claims
