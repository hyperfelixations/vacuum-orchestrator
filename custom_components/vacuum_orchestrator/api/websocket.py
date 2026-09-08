"""Authenticated versioned WebSocket queries and live state subscription."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.websocket_api import async_register_command
from homeassistant.components.websocket_api.connection import ActiveConnection
from homeassistant.components.websocket_api.decorators import (
    async_response,
    websocket_command,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv

from ..const import API_VERSION
from ..domain.errors import OrchestratorError
from ..runtime import async_get_runtime
from .presentation import present_job, present_queue

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
            present_job(state.jobs[job_id], orchestrator.readiness_for_job(job_id))
            for job_id in selected
        ]
        connection.send_result(
            msg["id"], present_queue(state, jobs, offset=offset, limit=limit)
        )
    except OrchestratorError as err:
        connection.send_error(msg["id"], err.code, str(err))


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
            connection.send_error(msg["id"], "unknown_job", "unknown_job")
            return
        readiness = (
            orchestrator.readiness_for_job(job.job_id)
            if job.job_id in orchestrator.state.queue
            else None
        )
        connection.send_result(msg["id"], present_job(job, readiness))
    except OrchestratorError as err:
        connection.send_error(msg["id"], err.code, str(err))


@websocket_command({vol.Required("type"): TYPE_JOBS_LIST, **PAGE_FIELDS})
@async_response
async def websocket_jobs_list(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> None:
    """Return bounded job-registry history ordered by creation time."""
    try:
        state = async_get_runtime(hass).orchestrator.state
        ordered = sorted(
            state.jobs.values(),
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
                "jobs": [present_job(job) for job in ordered[offset : offset + limit]],
            },
        )
    except OrchestratorError as err:
        connection.send_error(msg["id"], err.code, str(err))


@websocket_command({vol.Required("type"): TYPE_SUBSCRIBE})
@callback
def websocket_subscribe(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> None:
    """Subscribe a card to lightweight commit notifications."""
    try:
        orchestrator = async_get_runtime(hass).orchestrator
    except OrchestratorError as err:
        connection.send_error(msg["id"], err.code, str(err))
        return

    @callback
    def state_changed() -> None:
        state = orchestrator.state
        connection.send_event(
            msg["id"],
            {
                "api_version": API_VERSION,
                "commit_id": state.commit_id,
                "queue_revision": state.queue_revision,
                "mode": state.mode.value,
                "pending_jobs": len(state.queue),
                "needs_attention": state.needs_attention,
            },
        )

    connection.subscriptions[msg["id"]] = orchestrator.subscribe(state_changed)
    connection.send_result(msg["id"])
