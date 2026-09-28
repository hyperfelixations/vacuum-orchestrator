"""Shared public configuration schemas and stable entity requirement bindings."""

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er

from .domain.errors import ValidationError
from .domain.requirements import StateRequirement
from .domain.types import OperationKind

REQUIREMENT_SCHEMA = vol.Schema(
    {
        vol.Required("entity_id"): cv.entity_id,
        vol.Optional("accepted_states", default=["on"]): vol.All(
            [cv.string], vol.Length(min=1, max=20)
        ),
        vol.Optional("max_age_seconds"): vol.Any(
            None, vol.All(vol.Coerce(float), vol.Range(min=0.001))
        ),
        vol.Optional("robot_id"): vol.Any(None, cv.string),
        vol.Optional("operation"): vol.Any(
            None, vol.In([item.value for item in OperationKind])
        ),
        vol.Optional("entity_registry_id"): vol.Any(None, cv.string),
    }
)


def normalize_requirements(hass: HomeAssistant, values: object) -> list[dict[str, Any]]:
    """Resolve stable identities once; explicit bindings never fall back on deletion."""
    try:
        raw = vol.Schema(vol.All([REQUIREMENT_SCHEMA], vol.Length(max=100)))(values)
    except vol.Invalid as err:
        raise ValidationError("invalid_requirements") from err
    result = []
    for value in raw:
        reference = value.get("entity_registry_id") or value["entity_id"]
        entity = er.async_get(hass).async_get(reference)
        if value.get("entity_registry_id") and entity is None:
            raise ValidationError("requirement_binding_missing")
        normalized = dict(value)
        normalized["entity_registry_id"] = entity.id if entity else None
        requirement_from_data(normalized)
        result.append(normalized)
    return result


def requirement_from_data(value: Mapping[str, Any]) -> StateRequirement:
    """Build one typed condition after configuration validation."""
    return StateRequirement(
        value["entity_id"],
        tuple(value.get("accepted_states", ["on"])),
        value.get("max_age_seconds"),
        value.get("robot_id"),
        OperationKind(value["operation"]) if value.get("operation") else None,
        value.get("entity_registry_id"),
    )
