"""Public room configuration independent of persisted runtime facts."""

from dataclasses import replace
from typing import Any, cast

import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er

from .configuration_values import (
    REQUIREMENT_SCHEMA,
    normalize_requirements,
    requirement_from_data,
)
from .domain.due import DueBasis
from .domain.errors import ValidationError
from .domain.rooms import Room, RoomBinding

DUE_SCHEMA = vol.Schema(
    {
        vol.Optional("basis"): vol.In([item.value for item in DueBasis]),
        vol.Optional("vacuum_seconds"): vol.Any(
            None, vol.All(vol.Coerce(float), vol.Range(min=0.001))
        ),
        vol.Optional("mop_seconds"): vol.Any(
            None, vol.All(vol.Coerce(float), vol.Range(min=0.001))
        ),
        vol.Optional("occupancy_entity_id"): vol.Any(None, cv.entity_id),
        vol.Optional("occupied_state"): cv.string,
        vol.Optional("unoccupied_state"): cv.string,
    }
)
BINDING_SCHEMA = vol.Schema(
    {
        vol.Required("robot_id"): cv.string,
        vol.Required("target_ids"): vol.All([cv.string], vol.Length(min=1, max=100)),
        vol.Optional("map_id"): vol.Any(None, cv.string),
    }
)
ROOM_PATCH_SCHEMA = vol.Schema(
    {
        vol.Optional("name"): cv.string,
        vol.Optional("area_id"): vol.Any(None, cv.string),
        vol.Optional("floor_id"): vol.Any(None, cv.string),
        vol.Optional("enabled"): bool,
        vol.Optional("follow_area_name"): bool,
        vol.Optional("bindings"): vol.All([BINDING_SCHEMA], vol.Length(max=100)),
        vol.Optional("requirements"): vol.All(
            [REQUIREMENT_SCHEMA], vol.Length(max=100)
        ),
        vol.Optional("due_policy"): DUE_SCHEMA,
    }
)


def normalize_room_patch(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate writable fields and retain stable occupancy/condition identities."""
    try:
        values = ROOM_PATCH_SCHEMA(data)
    except vol.Invalid as err:
        raise ValidationError("invalid_room_configuration") from err
    if (
        values.get("area_id")
        and ar.async_get(hass).async_get_area(values["area_id"]) is None
    ):
        raise ValidationError("unknown_area")
    if "requirements" in values:
        values["requirements"] = tuple(
            requirement_from_data(item)
            for item in normalize_requirements(hass, values["requirements"])
        )
    if "bindings" in values:
        values["bindings"] = tuple(
            RoomBinding(item["robot_id"], tuple(item["target_ids"]), item.get("map_id"))
            for item in values["bindings"]
        )
    if "due_policy" in values:
        policy = dict(values["due_policy"])
        if "basis" in policy:
            policy["basis"] = DueBasis(policy["basis"])
        if "occupancy_entity_id" in policy:
            source = policy["occupancy_entity_id"]
            entity = er.async_get(hass).async_get(source) if source else None
            policy["occupancy_entity_registry_id"] = entity.id if entity else None
        values["due_policy"] = policy
    return cast(dict[str, Any], values)


def apply_room_patch(room: Room, values: dict[str, Any]) -> Room:
    """Merge policy fields under the global writer lock."""
    changes = dict(values)
    if "due_policy" in changes:
        changes["due_policy"] = replace(room.due_policy, **changes["due_policy"])
    if "name" in changes and "follow_area_name" not in changes:
        changes["follow_area_name"] = False
    if "area_id" in changes:
        changes["area_missing"] = False
    return replace(room, **changes)
