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
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers import config_validation as cv
from homeassistant.util.json import JsonObjectType, JsonValueType

from ..const import (
    API_VERSION,
    DOMAIN,
    SERVICE_CANCEL_JOB,
    SERVICE_CREATE_JOB,
    SERVICE_DELETE_JOB,
    SERVICE_END_QUEUE,
    SERVICE_GET_JOB,
    SERVICE_GET_QUEUE,
    SERVICE_MOVE_JOB,
    SERVICE_PAUSE_QUEUE,
    SERVICE_RESUME_QUEUE,
    SERVICE_RETRY_JOB,
    SERVICE_RETURN_ROBOT,
    SERVICE_RUN_QUEUE,
    SERVICE_START_JOB,
    SERVICE_UPDATE_JOB,
)
from ..domain.errors import ConflictError, OrchestratorError
from ..domain.intents import JobIntentPatch
from ..domain.types import (
    MoveDirection,
    QueueMode,
    SettingsPolicy,
)
from ..ha_context import request_context
from ..runtime import VacuumOrchestratorRuntime, async_get_runtime
from .configuration import setup_configuration_actions
from .errors import service_error
from .job_input import (
    CREATE_SCHEMA,
    _mode,
    areas,
    intent_from_data,
    mop_route,
    selected_areas,
    vacuum_level,
    water_level,
)
from .presentation import present_job, present_queue, present_settings, view_metadata
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
        vol.Optional(ATTR_AREAS): areas,
        vol.Optional(ATTR_MODE): _mode,
        vol.Optional(ATTR_NAME): vol.Any(None, cv.string),
        vol.Optional(ATTR_VACUUM_POWER): vol.Any(None, vacuum_level),
        vol.Optional(ATTR_MOP_INTENSITY): vol.Any(None, water_level),
        vol.Optional(ATTR_MOP_ROUTE): vol.Any(None, mop_route),
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
ATTR_AFTER_CANCEL = "after_cancel"
CANCEL_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_JOB_ID): cv.string,
        vol.Optional(ATTR_AFTER_CANCEL, default="stay"): vol.In(
            ("stay", "return_to_dock")
        ),
    }
)
ROBOT_SCHEMA = vol.Schema({vol.Required(ATTR_ROBOT_ID): cv.string})
ATTR_RUNNING_JOBS = "running_jobs"
END_QUEUE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_RUNNING_JOBS, default="finish"): vol.In(("finish", "cancel")),
        vol.Optional(ATTR_AFTER_CANCEL, default="stay"): vol.In(
            ("stay", "return_to_dock")
        ),
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
            runtime.orchestrator.async_create_job(
                intent_from_data(
                    dict(call.data),
                    runtime.orchestrator.eligible_room_ids,
                    runtime.orchestrator.state.job_defaults,
                )
            )
        )
        return _command_response(call, runtime, {ATTR_JOB_ID: job_id})

    async def update_job(call: ServiceCall) -> ServiceResponse | None:
        runtime = await _runtime_for_call(hass, call)
        await _translate_errors(
            runtime.orchestrator.async_update_job(
                call.data[ATTR_JOB_ID], _patch_from_call(runtime, call)
            )
        )
        return _command_response(call, runtime, {ATTR_JOB_ID: call.data[ATTR_JOB_ID]})

    async def delete_job(call: ServiceCall) -> ServiceResponse | None:
        runtime = await _runtime_for_call(hass, call)
        await _translate_errors(
            runtime.orchestrator.async_delete_job(call.data[ATTR_JOB_ID])
        )
        return _command_response(call, runtime, {ATTR_JOB_ID: call.data[ATTR_JOB_ID]})

    async def move_job(call: ServiceCall) -> ServiceResponse | None:
        runtime = await _runtime_for_call(hass, call)
        await _translate_errors(
            runtime.orchestrator.async_move_job(
                call.data[ATTR_JOB_ID], call.data[ATTR_DIRECTION]
            )
        )
        return _command_response(call, runtime, {ATTR_JOB_ID: call.data[ATTR_JOB_ID]})

    async def start_job(call: ServiceCall) -> ServiceResponse | None:
        runtime = await _runtime_for_call(hass, call)
        assignment = await _translate_errors(
            runtime.orchestrator.async_start_job(
                call.data[ATTR_JOB_ID], call.data.get(ATTR_ROBOT_ID)
            )
        )
        return _command_response(
            call,
            runtime,
            {
                ATTR_JOB_ID: call.data[ATTR_JOB_ID],
                ATTR_ROBOT_ID: assignment.robot_id,
                "settings": cast(JsonValueType, present_settings(assignment.settings)),
            },
        )

    async def run_queue(call: ServiceCall) -> ServiceResponse | None:
        runtime = await _runtime_for_call(hass, call)
        assignments = await _translate_errors(runtime.orchestrator.async_run_queue())
        return _command_response(
            call,
            runtime,
            {
                "dispatched": len(assignments),
                "robot_ids": [item.robot_id for item in assignments],
            },
        )

    async def pause_queue(call: ServiceCall) -> ServiceResponse | None:
        runtime = await _runtime_for_call(hass, call)
        await _translate_errors(
            runtime.orchestrator.async_set_queue_mode(QueueMode.PAUSED)
        )
        return _command_response(
            call, runtime, {ATTR_MODE: runtime.orchestrator.state.mode.value}
        )

    async def resume_queue(call: ServiceCall) -> ServiceResponse | None:
        return await run_queue(call)

    async def end_queue(call: ServiceCall) -> ServiceResponse | None:
        runtime = await _runtime_for_call(hass, call)
        await _translate_errors(
            runtime.orchestrator.async_end_queue(
                cancel_running=call.data[ATTR_RUNNING_JOBS] == "cancel",
                return_to_dock=call.data[ATTR_AFTER_CANCEL] == "return_to_dock",
            )
        )
        return _command_response(
            call, runtime, {ATTR_MODE: runtime.orchestrator.state.mode.value}
        )

    async def cancel_job(call: ServiceCall) -> ServiceResponse | None:
        runtime = await _runtime_for_call(hass, call)
        await _translate_errors(
            runtime.orchestrator.async_cancel_job(
                call.data[ATTR_JOB_ID],
                return_to_dock=call.data[ATTR_AFTER_CANCEL] == "return_to_dock",
            )
        )
        return _command_response(call, runtime, {ATTR_JOB_ID: call.data[ATTR_JOB_ID]})

    async def return_robot(call: ServiceCall) -> ServiceResponse | None:
        runtime = await _runtime_for_call(hass, call)
        await _translate_errors(
            runtime.orchestrator.async_return_robot(call.data[ATTR_ROBOT_ID])
        )
        return _command_response(
            call, runtime, {ATTR_ROBOT_ID: call.data[ATTR_ROBOT_ID]}
        )

    async def retry_job(call: ServiceCall) -> ServiceResponse | None:
        runtime = await _runtime_for_call(hass, call)
        job_id = await _translate_errors(
            runtime.orchestrator.async_retry_job(call.data[ATTR_JOB_ID])
        )
        return _command_response(call, runtime, {ATTR_JOB_ID: job_id})

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
            present_queue(state, jobs, offset=offset, limit=limit)
            | view_metadata(runtime.orchestrator),
        )

    async def get_job(call: ServiceCall) -> ServiceResponse:
        runtime = await _runtime_for_call(hass, call, require_admin=False)
        state = runtime.orchestrator.state
        job = state.jobs.get(call.data[ATTR_JOB_ID])
        if job is None:
            raise service_error(ConflictError("unknown_job", call.data[ATTR_JOB_ID]))
        return cast(
            ServiceResponse,
            present_job(
                job,
                runtime.orchestrator.readiness_before_start(job.job_id),
                state.room_registry.rooms,
                state.attempts,
            )
            | view_metadata(runtime.orchestrator),
        )

    _register(hass, SERVICE_CREATE_JOB, create_job, CREATE_SCHEMA)
    _register(hass, SERVICE_UPDATE_JOB, update_job, UPDATE_SCHEMA)
    _register(hass, SERVICE_DELETE_JOB, delete_job, JOB_SCHEMA)
    _register(hass, SERVICE_MOVE_JOB, move_job, MOVE_SCHEMA)
    _register(hass, SERVICE_START_JOB, start_job, START_SCHEMA)
    _register(hass, SERVICE_RUN_QUEUE, run_queue, EMPTY_SCHEMA)
    _register(hass, SERVICE_PAUSE_QUEUE, pause_queue, EMPTY_SCHEMA)
    _register(hass, SERVICE_RESUME_QUEUE, resume_queue, EMPTY_SCHEMA)
    _register(hass, SERVICE_END_QUEUE, end_queue, END_QUEUE_SCHEMA)
    _register(hass, SERVICE_CANCEL_JOB, cancel_job, CANCEL_SCHEMA)
    _register(hass, SERVICE_RETURN_ROBOT, return_robot, ROBOT_SCHEMA)
    _register(hass, SERVICE_RETRY_JOB, retry_job, JOB_SCHEMA)
    _register_query(hass, SERVICE_GET_QUEUE, get_queue, GET_QUEUE_SCHEMA)
    _register_query(hass, SERVICE_GET_JOB, get_job, JOB_SCHEMA)


def _register(hass: HomeAssistant, name: str, handler: Any, schema: vol.Schema) -> None:
    async def contextual(call: ServiceCall) -> ServiceResponse | None:
        runtime = await _runtime_for_call(hass, call)
        with (
            request_context(call.context),
            command_trace(runtime.orchestrator.trace, name, call.data),
        ):
            try:
                return cast(ServiceResponse | None, await handler(call))
            except OrchestratorError as err:
                raise service_error(err) from err

    hass.services.async_register(
        DOMAIN,
        name,
        contextual,
        schema=schema,
        supports_response=SupportsResponse.OPTIONAL,
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
        raise service_error(err) from err


def _patch_from_call(
    runtime: VacuumOrchestratorRuntime, call: ServiceCall
) -> JobIntentPatch:
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
            value = selected_areas(value, runtime.orchestrator.eligible_room_ids)
        elif public_name in {ATTR_REQUIRED_ON, ATTR_REQUIRED_OFF}:
            value = tuple(value)
        values[field_name] = value
    return JobIntentPatch(**values)  # type: ignore[arg-type]


def _command_response(
    call: ServiceCall, runtime: VacuumOrchestratorRuntime, ids: JsonObjectType
) -> ServiceResponse | None:
    if not call.return_response:
        return None
    return {
        "api_version": API_VERSION,
        "commit_id": runtime.orchestrator.state.commit_id,
        **ids,
    }


async def _translate_errors(awaitable: Any) -> Any:
    try:
        return await awaitable
    except OrchestratorError as err:
        raise service_error(err) from err
