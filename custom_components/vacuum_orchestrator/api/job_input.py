"""Shared public job-intent schema for jobs and stored templates."""

from typing import Any

import voluptuous as vol
from homeassistant.helpers import config_validation as cv

from ..domain.errors import ValidationError
from ..domain.intents import CleaningPreferences, JobIntent, TargetRef
from ..domain.types import MopRoute, SemanticLevel, SettingsPolicy, parse_cleaning_mode

ATTR_JOB_ID = "job_id"
ATTR_AREAS = "areas"
ATTR_MODE = "mode"
ATTR_NAME = "name"
ATTR_VACUUM_POWER = "vacuum_power"
ATTR_MOP_INTENSITY = "mop_intensity"
ATTR_MOP_ROUTE = "mop_route"
ATTR_PASSES = "passes"
ATTR_SOURCE = "source"
ATTR_REASON = "reason"
ATTR_NOTE = "note"
ATTR_DEDUPE_KEY = "dedupe_key"
ATTR_REQUIRED_ON = "required_on"
ATTR_REQUIRED_OFF = "required_off"
ATTR_SETTINGS_POLICY = "settings_policy"
ATTR_DIRECTION = "direction"
ATTR_ROBOT_ID = "robot_id"
ATTR_OFFSET = "offset"
ATTR_LIMIT = "limit"


def _mode(value: object) -> object:
    try:
        return parse_cleaning_mode(value)
    except ValidationError as err:
        raise vol.Invalid(str(err)) from err


INTENT_FIELDS: dict[Any, Any] = {
    vol.Required(ATTR_AREAS): vol.All(cv.ensure_list, [cv.string]),
    vol.Required(ATTR_MODE): _mode,
    vol.Optional(ATTR_NAME): cv.string,
    vol.Optional(ATTR_VACUUM_POWER): vol.Coerce(SemanticLevel),
    vol.Optional(ATTR_MOP_INTENSITY): vol.Coerce(SemanticLevel),
    vol.Optional(ATTR_MOP_ROUTE): vol.Coerce(MopRoute),
    vol.Optional(ATTR_PASSES, default=1): vol.All(
        vol.Coerce(int), vol.Range(min=1, max=10)
    ),
    vol.Optional(ATTR_SOURCE): cv.string,
    vol.Optional(ATTR_REASON): cv.string,
    vol.Optional(ATTR_NOTE): cv.string,
    vol.Optional(ATTR_DEDUPE_KEY): cv.string,
    vol.Optional(ATTR_REQUIRED_ON, default=[]): vol.All(cv.ensure_list, [cv.entity_id]),
    vol.Optional(ATTR_REQUIRED_OFF, default=[]): vol.All(
        cv.ensure_list, [cv.entity_id]
    ),
    vol.Optional(ATTR_SETTINGS_POLICY, default=SettingsPolicy.BEST_EFFORT): vol.Coerce(
        SettingsPolicy
    ),
}
CREATE_SCHEMA = vol.Schema(INTENT_FIELDS)


def intent_from_data(data: dict[str, Any]) -> JobIntent:
    """Build the canonical intent from validated public fields."""
    return JobIntent(
        areas=tuple(TargetRef(area_id) for area_id in data[ATTR_AREAS]),
        mode=data[ATTR_MODE],
        name=data.get(ATTR_NAME),
        preferences=CleaningPreferences(
            data.get(ATTR_VACUUM_POWER),
            data.get(ATTR_MOP_INTENSITY),
            data.get(ATTR_MOP_ROUTE),
        ),
        passes=data[ATTR_PASSES],
        source=data.get(ATTR_SOURCE),
        reason=data.get(ATTR_REASON),
        note=data.get(ATTR_NOTE),
        dedupe_key=data.get(ATTR_DEDUPE_KEY),
        required_on=tuple(data[ATTR_REQUIRED_ON]),
        required_off=tuple(data[ATTR_REQUIRED_OFF]),
        settings_policy=data[ATTR_SETTINGS_POLICY],
    )
