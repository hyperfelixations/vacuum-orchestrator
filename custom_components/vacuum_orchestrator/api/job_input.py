"""Shared public job-intent schema for jobs and stored templates."""

from collections.abc import Callable, Mapping
from typing import Any

import probatio
from homeassistant.helpers import config_validation as cv

from ..domain.errors import ValidationError, located
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
ATTR_ROOMS = "rooms"
ATTR_ALL_ROOMS = "all_rooms"
SELECTION = (ATTR_AREAS, ATTR_ROOMS, ATTR_ALL_ROOMS)


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


references = probatio.All(cv.ensure_list, [cv.string])


def duration_seconds(value: object) -> float:
    """Accept seconds or a duration such as `{minutes: 15}` or `"00:15:00"`."""
    return float(cv.time_period(value).total_seconds())


# Room selection; see dev doc "Actions".
SELECTION_FIELDS: dict[Any, Any] = {
    probatio.Optional(ATTR_AREAS): references,
    probatio.Optional(ATTR_ROOMS): references,
    probatio.Optional(ATTR_ALL_ROOMS): cv.boolean,
}
INTENT_FIELDS: dict[Any, Any] = {
    **SELECTION_FIELDS,
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
        **SELECTION_FIELDS,
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


Resolve = Callable[[str], str]


def selected_rooms(
    data: Mapping[str, Any],
    active_rooms: Callable[[], tuple[str, ...]],
    resolve: Resolve,
) -> tuple[tuple[TargetRef, ...], bool]:
    """Snapshot all active rooms or resolve the named areas and rooms in order.

    `resolve` names the room of an area or room ID and raises at that field.
    """
    if data.get(ATTR_ALL_ROOMS):
        if data.get(ATTR_AREAS) or data.get(ATTR_ROOMS):
            raise ValidationError("all_rooms_with_selection", path=(ATTR_ALL_ROOMS,))
        return tuple(TargetRef(room_id) for room_id in active_rooms()), True
    room_ids: list[str] = []
    for name in (ATTR_AREAS, ATTR_ROOMS):
        for index, reference in enumerate(data.get(name, ())):
            with located(name, index):
                room_ids.append(resolve(reference))
    return tuple(TargetRef(room_id) for room_id in dict.fromkeys(room_ids)), False


def intent_from_data(
    data: dict[str, Any],
    active_rooms: Callable[[], tuple[str, ...]],
    defaults: JobDefaults,
    references: EntityReferences,
    resolve: Resolve,
) -> JobIntent:
    """Build the canonical intent; unnamed values come from the job defaults."""
    targets, all_rooms = selected_rooms(data, active_rooms, resolve)
    return JobIntent(
        areas=targets,
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
        all_rooms=all_rooms,
    )


def patch_from_data(
    data: Mapping[str, Any],
    active_rooms: Callable[[], tuple[str, ...]],
    references: EntityReferences,
    resolve: Resolve,
) -> JobIntentPatch:
    """Build a job update from the fields an update names."""
    names = {
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
    if any(name in data for name in SELECTION):
        values["areas"], values["all_rooms"] = selected_rooms(
            data, active_rooms, resolve
        )
    for public_name, field_name in names.items():
        if public_name not in data:
            continue
        value = data[public_name]
        if public_name in {ATTR_REQUIRED_ON, ATTR_REQUIRED_OFF}:
            value = tuple(map(references.reference, value))
        values[field_name] = value
    return JobIntentPatch(**values)  # type: ignore[arg-type]
