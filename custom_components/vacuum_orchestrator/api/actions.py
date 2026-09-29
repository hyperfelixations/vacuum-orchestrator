"""Authenticated Home Assistant actions for commands and bounded queries."""

from __future__ import annotations

from typing import Any, cast

import voluptuous as vol
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import ServiceValidationError, Unauthorized
from homeassistant.helpers import config_validation as cv

from ..const import (
    DOMAIN,
    SERVICE_CANCEL_JOB,
    SERVICE_CREATE_JOB,
    SERVICE_DELETE_JOB,
    SERVICE_GET_JOB,
    SERVICE_GET_QUEUE,
    SERVICE_MOVE_JOB,
    SERVICE_PAUSE_QUEUE,
    SERVICE_RESUME_QUEUE,
    SERVICE_RETRY_JOB,
    SERVICE_RUN_QUEUE,
    SERVICE_START_JOB,
    SERVICE_UPDATE_JOB,
)
from ..domain.errors import OrchestratorError
from ..domain.intents import (
    JobIntent,
    JobIntentPatch,
    TargetRef,
)
from ..domain.types import (
    MopRoute,
    MoveDirection,
    QueueMode,
    SemanticLevel,
    SettingsPolicy,
)
from ..ha_context import request_context
from ..runtime import VacuumOrchestratorRuntime, async_get_runtime
from .configuration import setup_configuration_actions
from .job_input import CREATE_SCHEMA, _mode, intent_from_data
from .presentation import present_job, present_queue
from .telemetry import command_trace

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


UPDATE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_JOB_ID): cv.string,
        vol.Optional(ATTR_AREAS): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional(ATTR_MODE): _mode,
        vol.Optional(ATTR_NAME): vol.Any(None, cv.string),
        vol.Optional(ATTR_VACUUM_POWER): vol.Any(None, vol.Coerce(SemanticLevel)),
        vol.Optional(ATTR_MOP_INTENSITY): vol.Any(None, vol.Coerce(SemanticLevel)),
        vol.Optional(ATTR_MOP_ROUTE): vol.Any(None, vol.Coerce(MopRoute)),
        vol.Optional(ATTR_PASSES): vol.All(vol.Coerce(int), vol.Range(min=1, max=10)),
        vol.Optional(ATTR_SOURCE): vol.Any(None, cv.string),
        vol.Optional(ATTR_REASON): vol.Any(None, cv.string),
        vol.Optional(ATTR_NOTE): vol.Any(None, cv.string),
        vol.Optional(ATTR_DEDUPE_KEY): vol.Any(None, cv.string),
        vol.Optional(ATTR_REQUIRED_ON): vol.All(cv.ensure_list, [cv.entity_id]),
        vol.Optional(ATTR_REQUIRED_OFF): vol.All(cv.ensure_list, [cv.entity_id]),
        vol.Optional(ATTR_SETTINGS_POLICY): vol.Coerce(SettingsPolicy),
    }
)
JOB_SCHEMA = vol.Schema({vol.Required(ATTR_JOB_ID): cv.string})
START_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_JOB_ID): cv.string,
        vol.Optional(ATTR_ROBOT_ID): cv.string,
    }
)
MOVE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_JOB_ID): cv.string,
        vol.Required(ATTR_DIRECTION): vol.Coerce(MoveDirection),
    }
)
EMPTY_SCHEMA = vol.Schema({})
GET_QUEUE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_OFFSET, default=0): vol.All(
            vol.Coerce(int), vol.Range(min=0)
        ),
        vol.Optional(ATTR_LIMIT, default=50): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=100)
        ),
    }
)


async def async_setup_actions(hass: HomeAssistant) -> None:
    """Register integration actions exactly once."""
    if hass.services.has_service(DOMAIN, SERVICE_CREATE_JOB):
        return
    setup_configuration_actions(hass)

    async def create_job(call: ServiceCall) -> ServiceResponse | None:
        runtime = await _runtime_for_call(hass, call)
        job_id = await _translate_errors(
            runtime.orchestrator.async_create_job(_intent_from_call(call))
        )
        return _optional_response(call, {ATTR_JOB_ID: job_id})

    async def update_job(call: ServiceCall) -> ServiceResponse | None:
        runtime = await _runtime_for_call(hass, call)
        await _translate_errors(
            runtime.orchestrator.async_update_job(
                call.data[ATTR_JOB_ID], _patch_from_call(call)
            )
        )
        return _optional_response(call, {ATTR_JOB_ID: call.data[ATTR_JOB_ID]})

    async def delete_job(call: ServiceCall) -> None:
        runtime = await _runtime_for_call(hass, call)
        await _translate_errors(
            runtime.orchestrator.async_delete_job(call.data[ATTR_JOB_ID])
        )

    async def move_job(call: ServiceCall) -> None:
        runtime = await _runtime_for_call(hass, call)
        await _translate_errors(
            runtime.orchestrator.async_move_job(
                call.data[ATTR_JOB_ID], call.data[ATTR_DIRECTION]
            )
        )

    async def start_job(call: ServiceCall) -> ServiceResponse | None:
        runtime = await _runtime_for_call(hass, call)
        assignment = await _translate_errors(
            runtime.orchestrator.async_start_job(
                call.data[ATTR_JOB_ID], call.data.get(ATTR_ROBOT_ID)
            )
        )
        return _optional_response(
            call,
            {
                ATTR_JOB_ID: call.data[ATTR_JOB_ID],
                ATTR_ROBOT_ID: assignment.robot_id,
                "omitted_preferences": list(assignment.preference_resolution.omitted),
            },
        )

    async def run_queue(call: ServiceCall) -> ServiceResponse | None:
        runtime = await _runtime_for_call(hass, call)
        assignments = await _translate_errors(runtime.orchestrator.async_run_queue())
        return _optional_response(
            call,
            {
                "dispatched": len(assignments),
                "robot_ids": [item.robot_id for item in assignments],
            },
        )

    async def pause_queue(call: ServiceCall) -> None:
        runtime = await _runtime_for_call(hass, call)
        await _translate_errors(
            runtime.orchestrator.async_set_queue_mode(QueueMode.PAUSED)
        )

    async def resume_queue(call: ServiceCall) -> ServiceResponse | None:
        return await run_queue(call)

    async def cancel_job(call: ServiceCall) -> None:
        runtime = await _runtime_for_call(hass, call)
        await _translate_errors(
            runtime.orchestrator.async_cancel_job(call.data[ATTR_JOB_ID])
        )

    async def retry_job(call: ServiceCall) -> ServiceResponse | None:
        runtime = await _runtime_for_call(hass, call)
        job_id = await _translate_errors(
            runtime.orchestrator.async_retry_job(call.data[ATTR_JOB_ID])
        )
        return _optional_response(call, {ATTR_JOB_ID: job_id})

    async def get_queue(call: ServiceCall) -> ServiceResponse:
        runtime = await _runtime_for_call(hass, call, require_admin=False)
        state = runtime.orchestrator.state
        offset = call.data[ATTR_OFFSET]
        limit = call.data[ATTR_LIMIT]
        selected = state.queue[offset : offset + limit]
        jobs = [
            present_job(
                state.jobs[job_id],
                runtime.orchestrator.readiness_for_job(job_id),
                state.room_registry.rooms,
            )
            for job_id in selected
        ]
        return cast(
            ServiceResponse,
            present_queue(state, jobs, offset=offset, limit=limit),
        )

    async def get_job(call: ServiceCall) -> ServiceResponse:
        runtime = await _runtime_for_call(hass, call, require_admin=False)
        state = runtime.orchestrator.state
        job = state.jobs.get(call.data[ATTR_JOB_ID])
        if job is None:
            raise ServiceValidationError("unknown_job")
        readiness = (
            runtime.orchestrator.readiness_for_job(job.job_id)
            if job.state.value == "queued"
            else None
        )
        return cast(
            ServiceResponse, present_job(job, readiness, state.room_registry.rooms)
        )

    _register(hass, SERVICE_CREATE_JOB, create_job, CREATE_SCHEMA, optional=True)
    _register(hass, SERVICE_UPDATE_JOB, update_job, UPDATE_SCHEMA, optional=True)
    _register(hass, SERVICE_DELETE_JOB, delete_job, JOB_SCHEMA)
    _register(hass, SERVICE_MOVE_JOB, move_job, MOVE_SCHEMA)
    _register(hass, SERVICE_START_JOB, start_job, START_SCHEMA, optional=True)
    _register(hass, SERVICE_RUN_QUEUE, run_queue, EMPTY_SCHEMA, optional=True)
    _register(hass, SERVICE_PAUSE_QUEUE, pause_queue, EMPTY_SCHEMA)
    _register(hass, SERVICE_RESUME_QUEUE, resume_queue, EMPTY_SCHEMA, optional=True)
    _register(hass, SERVICE_CANCEL_JOB, cancel_job, JOB_SCHEMA)
    _register(hass, SERVICE_RETRY_JOB, retry_job, JOB_SCHEMA, optional=True)
    _register_query(hass, SERVICE_GET_QUEUE, get_queue, GET_QUEUE_SCHEMA)
    _register_query(hass, SERVICE_GET_JOB, get_job, JOB_SCHEMA)


def _register(
    hass: HomeAssistant,
    name: str,
    handler: Any,
    schema: vol.Schema,
    *,
    optional: bool = False,
) -> None:
    async def contextual(call: ServiceCall) -> ServiceResponse | None:
        runtime = await _runtime_for_call(hass, call)
        with (
            request_context(call.context),
            command_trace(runtime.orchestrator.trace, name, call.data),
        ):
            try:
                return cast(ServiceResponse | None, await handler(call))
            except OrchestratorError as err:
                raise ServiceValidationError(err.code) from err

    hass.services.async_register(
        DOMAIN,
        name,
        contextual,
        schema=schema,
        supports_response=(
            SupportsResponse.OPTIONAL if optional else SupportsResponse.NONE
        ),
    )


def _register_query(
    hass: HomeAssistant, name: str, handler: Any, schema: vol.Schema
) -> None:
    hass.services.async_register(
        DOMAIN,
        name,
        handler,
        schema=schema,
        supports_response=SupportsResponse.ONLY,
    )


async def _runtime_for_call(
    hass: HomeAssistant, call: ServiceCall, *, require_admin: bool = True
) -> VacuumOrchestratorRuntime:
    if require_admin and call.context.user_id is not None:
        user = await hass.auth.async_get_user(call.context.user_id)
        if user is None or not user.is_admin:
            raise Unauthorized(context=call.context)
    try:
        return async_get_runtime(hass)
    except OrchestratorError as err:
        raise ServiceValidationError(str(err)) from err


def _intent_from_call(call: ServiceCall) -> JobIntent:
    return intent_from_data(dict(call.data))


def _patch_from_call(call: ServiceCall) -> JobIntentPatch:
    names = {
        ATTR_AREAS: "areas",
        ATTR_MODE: "mode",
        ATTR_NAME: "name",
        ATTR_VACUUM_POWER: "vacuum_power",
        ATTR_MOP_INTENSITY: "mop_intensity",
        ATTR_MOP_ROUTE: "mop_route",
        ATTR_PASSES: "passes",
        ATTR_SOURCE: "source",
        ATTR_REASON: "reason",
        ATTR_NOTE: "note",
        ATTR_DEDUPE_KEY: "dedupe_key",
        ATTR_REQUIRED_ON: "required_on",
        ATTR_REQUIRED_OFF: "required_off",
        ATTR_SETTINGS_POLICY: "settings_policy",
    }
    values: dict[str, object] = {}
    for public_name, field_name in names.items():
        if public_name not in call.data:
            continue
        value = call.data[public_name]
        if public_name == ATTR_AREAS:
            value = tuple(TargetRef(area_id) for area_id in value)
        elif public_name in {ATTR_REQUIRED_ON, ATTR_REQUIRED_OFF}:
            value = tuple(value)
        values[field_name] = value
    return JobIntentPatch(**values)  # type: ignore[arg-type]


def _optional_response(
    call: ServiceCall, response: ServiceResponse
) -> ServiceResponse | None:
    return response if call.return_response else None


async def _translate_errors(awaitable: Any) -> Any:
    try:
        return await awaitable
    except OrchestratorError as err:
        raise ServiceValidationError(str(err)) from err
