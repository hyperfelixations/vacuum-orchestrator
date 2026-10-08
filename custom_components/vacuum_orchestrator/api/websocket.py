"""Authenticated versioned WebSocket queries and live state subscription."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.websocket_api import async_register_command
from homeassistant.components.websocket_api.connection import ActiveConnection
from homeassistant.components.websocket_api.decorators import (
    async_response,
    require_admin,
    websocket_command,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from ..const import API_VERSION, SIGNAL_VIEW_CHANGED
from ..domain.errors import ConflictError, OrchestratorError, ValidationError
from ..domain.types import JobState
from ..ha_context import request_context
from ..runtime import async_get_runtime
from .configuration import (
    COMMANDS,
    QUERIES,
    SESSION_COMMANDS,
    async_query_configuration,
    execute_configuration,
)
from .errors import send_websocket_error
from .presentation import present_job, present_queue, view_metadata

TYPE_QUEUE_GET = "vacuum_orchestrator/queue/get"
TYPE_JOB_GET = "vacuum_orchestrator/job/get"
TYPE_JOBS_LIST = "vacuum_orchestrator/jobs/list"
TYPE_SUBSCRIBE = "vacuum_orchestrator/subscribe"

PAGE_FIELDS: dict[Any, Any] = {
    vol.Optional("offset", default=0): vol.All(vol.Coerce(int), vol.Range(min=0)),
    vol.Optional("limit", default=50): vol.All(
        vol.Coerce(int), vol.Range(min=1, max=100)
    ),
}


def async_setup_websocket(hass: HomeAssistant) -> None:
    """Register the complete read-side API once during integration setup."""
    async_register_command(hass, websocket_queue_get)
    async_register_command(hass, websocket_job_get)
    async_register_command(hass, websocket_jobs_list)
    async_register_command(hass, websocket_subscribe)
    async_register_command(hass, websocket_configuration_get)
    async_register_command(hass, websocket_configuration_command)


@websocket_command(
    {
        vol.Required("type"): "vacuum_orchestrator/configuration/get",
        vol.Required("query"): vol.In(QUERIES),
        vol.Optional("parameters", default={}): dict,
    }
)
@async_response
async def websocket_configuration_get(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> None:
    """Query room and configuration views through their action schemas."""
    try:
        data = QUERIES[msg["query"]](msg["parameters"])
        connection.send_result(
            msg["id"], await async_query_configuration(hass, msg["query"], data)
        )
    except vol.Invalid as err:
        send_websocket_error(
            connection, msg["id"], ValidationError("invalid_parameters", str(err))
        )
    except OrchestratorError as err:
        send_websocket_error(connection, msg["id"], err)


@websocket_command(
    {
        vol.Required("type"): "vacuum_orchestrator/configuration/command",
        vol.Required("command"): vol.In({**COMMANDS, **SESSION_COMMANDS}),
        vol.Required("parameters"): dict,
    }
)
@require_admin
@async_response
async def websocket_configuration_command(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> None:
    """Use the same validated commands for HA and optional clients."""
    try:
        data = {**COMMANDS, **SESSION_COMMANDS}[msg["command"]](msg["parameters"])
        with request_context(connection.context(msg)):
            connection.send_result(
                msg["id"], await execute_configuration(hass, msg["command"], data)
            )
    except vol.Invalid as err:
        send_websocket_error(
            connection, msg["id"], ValidationError("invalid_parameters", str(err))
        )
    except OrchestratorError as err:
        send_websocket_error(connection, msg["id"], err)


@websocket_command({vol.Required("type"): TYPE_QUEUE_GET, **PAGE_FIELDS})
@async_response
async def websocket_queue_get(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> None:
    """Return one pending-queue page."""
    try:
        orchestrator = async_get_runtime(hass).orchestrator
        state = orchestrator.state
        offset = msg["offset"]
        limit = msg["limit"]
        selected = state.queue[offset : offset + limit]
        jobs = [
            present_job(
                state.jobs[job_id],
                orchestrator.entity_references,
                orchestrator.readiness_for_job(job_id),
                state.room_registry.rooms,
                hold=orchestrator.job_hold(job_id),
            )
            for job_id in selected
        ]
        connection.send_result(
            msg["id"],
            present_queue(state, jobs, offset=offset, limit=limit)
            | view_metadata(orchestrator),
        )
    except OrchestratorError as err:
        send_websocket_error(connection, msg["id"], err)


@websocket_command(
    {vol.Required("type"): TYPE_JOB_GET, vol.Required("job_id"): cv.string}
)
@async_response
async def websocket_job_get(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> None:
    """Return one complete public job representation."""
    try:
        orchestrator = async_get_runtime(hass).orchestrator
        job = orchestrator.state.jobs.get(msg["job_id"])
        if job is None:
            raise ConflictError("unknown_job", msg["job_id"])
        connection.send_result(
            msg["id"],
            present_job(
                job,
                orchestrator.entity_references,
                orchestrator.readiness_before_start(job.job_id),
                orchestrator.state.room_registry.rooms,
                orchestrator.state.attempts,
                hold=orchestrator.job_hold(job.job_id),
            )
            | view_metadata(orchestrator),
        )
    except OrchestratorError as err:
        send_websocket_error(connection, msg["id"], err)


@websocket_command(
    {
        vol.Required("type"): TYPE_JOBS_LIST,
        **PAGE_FIELDS,
        vol.Optional("states"): vol.All(
            cv.ensure_list, [vol.Coerce(JobState)], vol.Length(min=1)
        ),
    }
)
@async_response
async def websocket_jobs_list(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> None:
    """Return bounded job-registry history ordered by creation time."""
    try:
        orchestrator = async_get_runtime(hass).orchestrator
        state = orchestrator.state
        states = set(msg.get("states", JobState))
        ordered = sorted(
            (job for job in state.jobs.values() if job.state in states),
            key=lambda job: (job.created_at, job.job_id),
            reverse=True,
        )
        offset = msg["offset"]
        limit = msg["limit"]
        connection.send_result(
            msg["id"],
            {
                "api_version": API_VERSION,
                "total": len(ordered),
                "offset": offset,
                "limit": limit,
                "jobs": [
                    present_job(
                        job,
                        orchestrator.entity_references,
                        rooms=state.room_registry.rooms,
                        attempts=state.attempts,
                        hold=orchestrator.job_hold(job.job_id),
                    )
                    for job in ordered[offset : offset + limit]
                ],
            }
            | view_metadata(orchestrator),
        )
    except OrchestratorError as err:
        send_websocket_error(connection, msg["id"], err)


@websocket_command({vol.Required("type"): TYPE_SUBSCRIBE})
@callback
def websocket_subscribe(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> None:
    """Subscribe to view changes of whichever runtime is loaded, across reloads."""

    @callback
    def state_changed() -> None:
        connection.send_event(msg["id"], _view_event(hass))

    connection.subscriptions[msg["id"]] = async_dispatcher_connect(
        hass, SIGNAL_VIEW_CHANGED, state_changed
    )
    connection.send_result(msg["id"])
    event = _view_event(hass)
    if not event["loaded"]:
        connection.send_event(msg["id"], event)


def _view_event(hass: HomeAssistant) -> dict[str, Any]:
    try:
        orchestrator = async_get_runtime(hass).orchestrator
    except OrchestratorError:
        return {"api_version": API_VERSION, "loaded": False}
    state = orchestrator.state
    return {
        "api_version": API_VERSION,
        "loaded": True,
        "commit_id": state.commit_id,
        "runtime_id": orchestrator.runtime_id,
        "runtime_sequence": orchestrator.runtime_sequence,
        "queue_revision": state.queue_revision,
        "mode": state.mode.value,
        "pending_jobs": len(state.queue),
        "needs_attention": state.needs_attention,
        "active_count": state.active_job_count,
        "attention_count": state.attention_job_count,
        "changed": sorted(orchestrator.changed_scopes),
    }
