"""Shared public configuration schemas and stable entity requirement bindings."""

from collections.abc import Mapping
from typing import Any

import probatio
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er

from .domain.errors import ValidationError
from .domain.requirements import StateRequirement
from .domain.types import OperationKind

REQUIREMENT_SCHEMA = probatio.Schema(
    {
        probatio.Required("entity_id"): cv.entity_id,
        probatio.Optional("accepted_states", default=["on"]): probatio.All(
            [cv.string], probatio.Length(min=1, max=20)
        ),
        probatio.Optional("max_age_seconds"): probatio.Any(
            None, probatio.All(probatio.Coerce(float), probatio.Range(min=0.001))
        ),
        probatio.Optional("robot_id"): probatio.Any(None, cv.string),
        probatio.Optional("operation"): probatio.Any(
            None, probatio.In([item.value for item in OperationKind])
        ),
        probatio.Optional("entity_registry_id"): probatio.Any(None, cv.string),
    }
)


def normalize_requirements(hass: HomeAssistant, values: object) -> list[dict[str, Any]]:
    """Resolve stable identities once; explicit bindings never fall back on deletion."""
    try:
        raw = probatio.Schema(
            probatio.All([REQUIREMENT_SCHEMA], probatio.Length(max=100))
        )(values)
    except probatio.Invalid as err:
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
