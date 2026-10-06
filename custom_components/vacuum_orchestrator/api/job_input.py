"""Shared public job-intent schema for jobs and stored templates."""

from collections.abc import Callable
from typing import Any

import voluptuous as vol
from homeassistant.helpers import config_validation as cv

from ..domain.errors import ValidationError
from ..domain.intents import CleaningPreferences, JobIntent, TargetRef
from ..domain.job_defaults import JobDefaults
from ..domain.types import (
    ROUTE_LADDER,
    VACUUM_LADDER,
    WATER_LADDER,
    SettingsPolicy,
    parse_cleaning_mode,
)

ATTR_JOB_ID = "job_id"
ATTR_AREAS = "areas"
ATTR_MODE = "mode"
ATTR_NAME = "name"
ATTR_VACUUM_POWER = "vacuum_power"
ATTR_MOP_INTENSITY = "mop_intensity"
ATTR_MOP_ROUTE = "mop_route"
ATTR_PASSES = "passes"
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
ALL_ROOMS = "all"


def _mode(value: object) -> object:
    try:
        return parse_cleaning_mode(value)
    except ValidationError as err:
        raise vol.Invalid(str(err)) from err


def _rung(ladder: tuple[Any, ...]) -> Callable[[object], Any]:
    """Accept only selectable rungs of one ordered setting ladder."""
    by_value = {item.value: item for item in ladder}

    def validate(value: object) -> Any:
        if isinstance(value, str) and value in by_value:
            return by_value[value]
        raise vol.Invalid(f"expected one of {', '.join(by_value)}")

    return validate


vacuum_level = _rung(VACUUM_LADDER)
water_level = _rung(WATER_LADDER)
mop_route = _rung(ROUTE_LADDER)


def areas(value: object) -> list[str] | str:
    """Accept room IDs or "all", alone or as the only list item."""
    selected = vol.All(cv.ensure_list, [cv.string])(value)
    return ALL_ROOMS if selected == [ALL_ROOMS] else selected


INTENT_FIELDS: dict[Any, Any] = {
    vol.Required(ATTR_AREAS): areas,
    vol.Optional(ATTR_MODE): _mode,
    vol.Optional(ATTR_NAME): cv.string,
    vol.Optional(ATTR_VACUUM_POWER): vacuum_level,
    vol.Optional(ATTR_MOP_INTENSITY): water_level,
    vol.Optional(ATTR_MOP_ROUTE): mop_route,
    vol.Optional(ATTR_PASSES): vol.All(vol.Coerce(int), vol.Range(min=1, max=10)),
    vol.Optional(ATTR_REASON): cv.string,
    vol.Optional(ATTR_NOTE): cv.string,
    vol.Optional(ATTR_DEDUPE_KEY): cv.string,
    vol.Optional(ATTR_REQUIRED_ON, default=[]): vol.All(cv.ensure_list, [cv.entity_id]),
    vol.Optional(ATTR_REQUIRED_OFF, default=[]): vol.All(
        cv.ensure_list, [cv.entity_id]
    ),
    vol.Optional(ATTR_SETTINGS_POLICY): vol.Coerce(SettingsPolicy),
}
CREATE_SCHEMA = vol.Schema(INTENT_FIELDS)


def selected_areas(
    value: list[str] | str, eligible_rooms: Callable[[], tuple[str, ...]]
) -> tuple[TargetRef, ...]:
    """Snapshot an all-rooms selection or keep the requested references."""
    room_ids = eligible_rooms() if value == ALL_ROOMS else value
    return tuple(TargetRef(room_id) for room_id in room_ids)


def intent_from_data(
    data: dict[str, Any],
    eligible_rooms: Callable[[], tuple[str, ...]],
    defaults: JobDefaults,
) -> JobIntent:
    """Build the canonical intent; unnamed values come from the job defaults."""
    return JobIntent(
        areas=selected_areas(data[ATTR_AREAS], eligible_rooms),
        mode=data.get(ATTR_MODE, defaults.mode),
        name=data.get(ATTR_NAME),
        preferences=CleaningPreferences(
            data.get(ATTR_VACUUM_POWER),
            data.get(ATTR_MOP_INTENSITY),
            data.get(ATTR_MOP_ROUTE),
        ),
        passes=data.get(ATTR_PASSES, defaults.passes),
        reason=data.get(ATTR_REASON),
        note=data.get(ATTR_NOTE),
        dedupe_key=data.get(ATTR_DEDUPE_KEY),
        required_on=tuple(data[ATTR_REQUIRED_ON]),
        required_off=tuple(data[ATTR_REQUIRED_OFF]),
        settings_policy=data.get(ATTR_SETTINGS_POLICY, defaults.settings_policy),
        all_rooms=data[ATTR_AREAS] == ALL_ROOMS,
    )
