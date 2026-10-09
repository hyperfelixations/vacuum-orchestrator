"""Shared public job-intent schema for jobs and stored templates."""

from collections.abc import Callable, Mapping
from typing import Any

import probatio
from homeassistant.helpers import config_validation as cv

from ..domain.errors import ValidationError
from ..domain.intents import CleaningPreferences, JobIntent, JobIntentPatch, TargetRef
from ..domain.job_defaults import JobDefaults
from ..domain.types import (
    ROUTE_LADDER,
    VACUUM_LADDER,
    WATER_LADDER,
    SettingsPolicy,
    parse_cleaning_mode,
)
from ..ports.entities import EntityReferences

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
ATTR_HOLD_ID = "hold_id"
ATTR_START = "start"
ALL_ROOMS = "all"


def _mode(value: object) -> object:
    try:
        return parse_cleaning_mode(value)
    except ValidationError as err:
        raise probatio.Invalid(str(err)) from err


def _rung(ladder: tuple[Any, ...]) -> Callable[[object], Any]:
    """Accept only selectable rungs of one ordered setting ladder."""
    by_value = {item.value: item for item in ladder}

    def validate(value: object) -> Any:
        if isinstance(value, str) and value in by_value:
            return by_value[value]
        raise probatio.Invalid(f"expected one of {', '.join(by_value)}")

    return validate


vacuum_level = _rung(VACUUM_LADDER)
water_level = _rung(WATER_LADDER)
mop_route = _rung(ROUTE_LADDER)


def areas(value: object) -> list[str] | str:
    """Accept room IDs or "all", alone or as the only list item."""
    selected = probatio.All(cv.ensure_list, [cv.string])(value)
    return ALL_ROOMS if selected == [ALL_ROOMS] else selected


INTENT_FIELDS: dict[Any, Any] = {
    probatio.Required(ATTR_AREAS): areas,
    probatio.Optional(ATTR_MODE): _mode,
    probatio.Optional(ATTR_NAME): cv.string,
    probatio.Optional(ATTR_VACUUM_POWER): vacuum_level,
    probatio.Optional(ATTR_MOP_INTENSITY): water_level,
    probatio.Optional(ATTR_MOP_ROUTE): mop_route,
    probatio.Optional(ATTR_PASSES): probatio.All(
        probatio.Coerce(int), probatio.Range(min=1, max=10)
    ),
    probatio.Optional(ATTR_REASON): cv.string,
    probatio.Optional(ATTR_NOTE): cv.string,
    probatio.Optional(ATTR_DEDUPE_KEY): cv.string,
    probatio.Optional(ATTR_REQUIRED_ON, default=[]): probatio.All(
        cv.ensure_list, [cv.entity_id]
    ),
    probatio.Optional(ATTR_REQUIRED_OFF, default=[]): probatio.All(
        cv.ensure_list, [cv.entity_id]
    ),
    probatio.Optional(ATTR_SETTINGS_POLICY): probatio.Coerce(SettingsPolicy),
}
CREATE_SCHEMA = probatio.Schema(INTENT_FIELDS)
CREATE_JOB_SCHEMA = CREATE_SCHEMA.extend(
    {
        probatio.Optional(ATTR_START, default=False): cv.boolean,
        probatio.Optional(ATTR_ROBOT_ID): cv.string,
    }
)
UPDATE_SCHEMA = probatio.Schema(
    {
        probatio.Required(ATTR_JOB_ID): cv.string,
        probatio.Optional(ATTR_AREAS): areas,
        probatio.Optional(ATTR_MODE): _mode,
        probatio.Optional(ATTR_NAME): probatio.Any(None, cv.string),
        probatio.Optional(ATTR_VACUUM_POWER): probatio.Any(None, vacuum_level),
        probatio.Optional(ATTR_MOP_INTENSITY): probatio.Any(None, water_level),
        probatio.Optional(ATTR_MOP_ROUTE): probatio.Any(None, mop_route),
        probatio.Optional(ATTR_PASSES): probatio.All(
            probatio.Coerce(int), probatio.Range(min=1, max=10)
        ),
        probatio.Optional(ATTR_REASON): probatio.Any(None, cv.string),
        probatio.Optional(ATTR_NOTE): probatio.Any(None, cv.string),
        probatio.Optional(ATTR_DEDUPE_KEY): probatio.Any(None, cv.string),
        probatio.Optional(ATTR_REQUIRED_ON): probatio.All(
            cv.ensure_list, [cv.entity_id]
        ),
        probatio.Optional(ATTR_REQUIRED_OFF): probatio.All(
            cv.ensure_list, [cv.entity_id]
        ),
        probatio.Optional(ATTR_SETTINGS_POLICY): probatio.Coerce(SettingsPolicy),
        probatio.Optional(ATTR_HOLD_ID): cv.string,
    }
)


def selected_areas(
    value: list[str] | str, active_rooms: Callable[[], tuple[str, ...]]
) -> tuple[TargetRef, ...]:
    """Snapshot an all-rooms selection or keep the requested references."""
    room_ids = active_rooms() if value == ALL_ROOMS else value
    return tuple(TargetRef(room_id) for room_id in room_ids)


def intent_from_data(
    data: dict[str, Any],
    active_rooms: Callable[[], tuple[str, ...]],
    defaults: JobDefaults,
    references: EntityReferences,
) -> JobIntent:
    """Build the canonical intent; unnamed values come from the job defaults."""
    return JobIntent(
        areas=selected_areas(data[ATTR_AREAS], active_rooms),
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
        required_on=tuple(map(references.reference, data[ATTR_REQUIRED_ON])),
        required_off=tuple(map(references.reference, data[ATTR_REQUIRED_OFF])),
        settings_policy=data.get(ATTR_SETTINGS_POLICY, defaults.settings_policy),
        all_rooms=data[ATTR_AREAS] == ALL_ROOMS,
    )


def patch_from_data(
    data: Mapping[str, Any],
    active_rooms: Callable[[], tuple[str, ...]],
    references: EntityReferences,
) -> JobIntentPatch:
    """Build a job update from the fields an update names."""
    names = {
        ATTR_AREAS: "areas",
        ATTR_MODE: "mode",
        ATTR_NAME: "name",
        ATTR_VACUUM_POWER: "vacuum_power",
        ATTR_MOP_INTENSITY: "mop_intensity",
        ATTR_MOP_ROUTE: "mop_route",
        ATTR_PASSES: "passes",
        ATTR_REASON: "reason",
        ATTR_NOTE: "note",
        ATTR_DEDUPE_KEY: "dedupe_key",
        ATTR_REQUIRED_ON: "required_on",
        ATTR_REQUIRED_OFF: "required_off",
        ATTR_SETTINGS_POLICY: "settings_policy",
    }
    values: dict[str, object] = {}
    for public_name, field_name in names.items():
        if public_name not in data:
            continue
        value = data[public_name]
        if public_name == ATTR_AREAS:
            values["all_rooms"] = value == ALL_ROOMS
            value = selected_areas(value, active_rooms)
        elif public_name in {ATTR_REQUIRED_ON, ATTR_REQUIRED_OFF}:
            value = tuple(map(references.reference, value))
        values[field_name] = value
    return JobIntentPatch(**values)  # type: ignore[arg-type]
