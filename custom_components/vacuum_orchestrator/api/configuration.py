"""Authenticated room, robot and recovery actions sharing canonical validators."""

from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any, cast

import voluptuous as vol
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import config_validation as cv

from ..adapters.discovery import discover_robots
from ..application.explanation import explain_job
from ..configuration import configure_robot, require_idle_robot
from ..const import API_VERSION, DOMAIN
from ..diagnostics import build_diagnostics
from ..domain.errors import OrchestratorError, ValidationError
from ..domain.releases import ReleaseKind
from ..domain.templates import JobTemplate
from ..ha_context import request_context
from ..room_configuration import (
    ROOM_PATCH_SCHEMA,
    apply_room_patch,
    normalize_room_patch,
)
from ..runtime import async_get_runtime
from .errors import service_error
from .job_input import CREATE_SCHEMA, intent_from_data
from .presentation import present_job, view_metadata
from .room_presentation import present_room
from .telemetry import command_trace

PAGE = {
    vol.Optional("offset", default=0): vol.All(vol.Coerce(int), vol.Range(min=0)),
    vol.Optional("limit", default=50): vol.All(
        vol.Coerce(int), vol.Range(min=1, max=100)
    ),
}
ROOM_ID: dict[Any, Any] = {vol.Required("room_id"): cv.string}
ROBOT_ID: dict[Any, Any] = {vol.Required("robot_id"): cv.string}
COMMANDS: dict[str, vol.Schema] = {
    "configure_queue": vol.Schema(
        {
            vol.Required("grace_seconds"): vol.All(
                vol.Coerce(float), vol.Range(min=0, max=86400)
            )
        }
    ),
    "save_template": vol.Schema(
        {
            vol.Optional("template_id"): cv.string,
            vol.Required("name"): cv.string,
            vol.Required("intent"): CREATE_SCHEMA,
            vol.Optional("enabled", default=True): bool,
            vol.Optional("automatic", default=False): bool,
        }
    ),
    "remove_template": vol.Schema({vol.Required("template_id"): cv.string}),
    "create_job_from_template": vol.Schema({vol.Required("template_id"): cv.string}),
    "reset_template_demand": vol.Schema({vol.Required("template_id"): cv.string}),
    "create_room": vol.Schema(
        {vol.Required("name"): cv.string, vol.Optional("area_id"): cv.string}
    ),
    "update_room": vol.Schema(
        {**ROOM_ID, vol.Required("configuration"): ROOM_PATCH_SCHEMA}
    ),
    "disable_room": vol.Schema(ROOM_ID),
    "enable_room": vol.Schema(ROOM_ID),
    "release_room": vol.Schema(
        {
            **ROOM_ID,
            vol.Required("kind"): vol.In([kind.value for kind in ReleaseKind]),
            vol.Optional("duration_seconds"): vol.All(
                vol.Coerce(float), vol.Range(min=0.001)
            ),
        }
    ),
    "revoke_room": vol.Schema(ROOM_ID),
    "add_robot": vol.Schema({vol.Required("configuration"): dict}),
    "configure_robot": vol.Schema({**ROBOT_ID, vol.Required("configuration"): dict}),
    "remove_robot": vol.Schema(ROBOT_ID),
    "resolve_recovery": vol.Schema(
        {**ROBOT_ID, vol.Optional("confirm_stopped", default=False): bool}
    ),
}
QUERIES: dict[str, vol.Schema] = {
    "get_job_execution": vol.Schema({vol.Required("job_id"): cv.string}),
    "get_trace": vol.Schema({**PAGE, vol.Optional("job_id"): cv.string}),
    "get_history": vol.Schema(PAGE),
    "get_diagnostics": vol.Schema({}),
    "get_templates": vol.Schema(PAGE),
    "get_rooms": vol.Schema(PAGE),
    "get_room": vol.Schema(ROOM_ID),
    "get_robots": vol.Schema(PAGE),
    "get_robot_candidates": vol.Schema(PAGE),
}


async def async_query_configuration(
    hass: HomeAssistant, name: str, data: dict[str, Any]
) -> dict[str, Any]:
    """Include live per-robot explanations without performing physical commands."""
    core = async_get_runtime(hass).orchestrator
    if name != "get_job_execution":
        return query_configuration(hass, name, data) | view_metadata(core)
    explanations = await explain_job(core, data["job_id"])
    job = core.state.jobs[data["job_id"]]
    return {
        "api_version": API_VERSION,
        "job_id": job.job_id,
        "robots": [
            {
                "robot_id": item.robot_id,
                "operation": item.operation.value,
                "readiness": present_job(job, item.readiness)["readiness"],
                "eligible": item.eligibility_reason is None
                and item.readiness.state.value == "ready",
                "eligibility_reason": item.eligibility_reason,
                "applied_preferences": list(item.preferences.applied)
                if item.preferences
                else [],
                "omitted_preferences": list(item.preferences.omitted)
                if item.preferences
                else [],
            }
            for item in explanations
        ],
        "attempts": [
            {
                "attempt_id": attempt.attempt_id,
                "work_unit_id": attempt.work_unit_id,
                "robot_id": attempt.robot_id,
                "state": attempt.state.value,
                "quality": attempt.completion_quality.value
                if attempt.completion_quality
                else None,
                "failure_code": attempt.failure_code,
                "applied_preferences": list(
                    core.state.assignments[
                        attempt.attempt_id
                    ].preference_resolution.applied
                ),
                "omitted_preferences": list(
                    core.state.assignments[
                        attempt.attempt_id
                    ].preference_resolution.omitted
                ),
            }
            for attempt in core.state.attempts.values()
            if attempt.job_id == job.job_id
        ],
    } | view_metadata(core)


def page(items: list[dict[str, Any]], data: dict[str, Any], key: str) -> dict[str, Any]:
    """Bound all collection responses consistently."""
    offset, limit = data["offset"], data["limit"]
    return {
        "api_version": API_VERSION,
        "total": len(items),
        "offset": offset,
        "limit": limit,
        key: items[offset : offset + limit],
    }


def query_configuration(
    hass: HomeAssistant, name: str, data: dict[str, Any]
) -> dict[str, Any]:
    """Read rich configuration models without invoking device services."""
    runtime = async_get_runtime(hass)
    core = runtime.orchestrator
    now = datetime.now(UTC)
    if name == "get_diagnostics":
        return build_diagnostics(runtime)
    if name == "get_trace":
        records = core.trace.snapshot(data.get("job_id"))
        result = page(list(reversed(records)), data, "records")
        result.update(runtime_id=core.runtime_id, trace_sequence=core.trace.sequence)
        return result
    if name == "get_history":
        runs = sorted(
            core.state.robot_runs.values(),
            key=lambda run: (
                run.observed_end or run.observed_start or now,
                run.robot_run_id,
            ),
            reverse=True,
        )
        return page(
            [
                {
                    "run_id": run.robot_run_id,
                    "source": run.source.value,
                    "operation": run.operation.value if run.operation else None,
                    "room_ids": list(run.canonical_targets),
                    "observed_start": run.observed_start.isoformat()
                    if run.observed_start
                    else None,
                    "observed_end": run.observed_end.isoformat()
                    if run.observed_end
                    else None,
                    "quality": run.completion_quality.value
                    if run.completion_quality
                    else None,
                    "failure_code": run.failure_code,
                }
                for run in runs
            ],
            data,
            "runs",
        )
    if name == "get_templates":
        return page(
            [present_template(value) for value in core.state.templates.values()],
            data,
            "templates",
        )
    if name == "get_room":
        return {
            "api_version": API_VERSION,
            **present_room(core.rooms.registry.resolve(data["room_id"]), now),
        }
    if name == "get_rooms":
        return page(
            [present_room(room, now) for room in core.rooms.registry.rooms.values()],
            data,
            "rooms",
        )
    if name == "get_robot_candidates":
        return page(
            [
                {
                    "registry_id": item.registry_id,
                    "entity_id": item.entity_id,
                    "name": item.name,
                    "adapter": item.adapter,
                    "roles": dict(item.roles),
                    "ambiguous_roles": list(item.ambiguous_roles),
                    "protocol": item.protocol,
                }
                for item in discover_robots(hass)
            ],
            data,
            "candidates",
        )
    controller = runtime.controller
    assert controller is not None
    robots = []
    for robot_id, subentry in controller.entry.subentries.items():
        adapter = core.adapters.get(robot_id)
        profile = adapter.profile if adapter else None
        robots.append(
            {
                "robot_id": robot_id,
                "name": subentry.title,
                "configuration": dict(subentry.data),
                "active": any(
                    lease.robot_id == robot_id
                    for lease in core.state.robot_leases.values()
                ),
                "blocked_reason": core.state.blocked_robots.get(profile.source_robot_id)
                if profile
                else None,
                "capabilities": None
                if profile is None
                else {
                    "revision": profile.capabilities.revision,
                    "operations": sorted(
                        item.value for item in profile.effective_operations
                    ),
                    "targets": {
                        key: list(profile.capabilities.targets_for(key))
                        for key in profile.capabilities.target_map
                    },
                    "map_context": profile.capabilities.map_context,
                    "maximum_passes": profile.capabilities.passes.maximum,
                    "vacuum_levels": sorted(
                        item.value for item in profile.capabilities.vacuum_levels
                    ),
                    "water_levels": sorted(
                        item.value for item in profile.capabilities.water_levels
                    ),
                    "mop_routes": sorted(
                        item.value for item in profile.capabilities.mop_routes
                    ),
                },
            }
        )
    return page(robots, data, "robots")


async def execute_configuration(
    hass: HomeAssistant, name: str, data: dict[str, Any]
) -> dict[str, Any]:
    """Apply one command with a runtime-local diagnostic request identity."""
    with command_trace(async_get_runtime(hass).orchestrator.trace, name, data):
        return await _execute_configuration(hass, name, data)


async def _execute_configuration(
    hass: HomeAssistant, name: str, data: dict[str, Any]
) -> dict[str, Any]:
    """Route mutations into room commands or the shared HA configuration service."""
    runtime = async_get_runtime(hass)
    core = runtime.orchestrator
    room_id = data.get("room_id")
    robot_id = data.get("robot_id")
    result: dict[str, Any] = {"api_version": API_VERSION}
    if name == "configure_queue":
        await core.runs.async_configure(data["grace_seconds"])
        result["grace_seconds"] = core.state.queue_grace_seconds
    elif name == "save_template":
        result["template_id"] = await core.templates.async_save(
            data["name"],
            intent_from_data(data["intent"]),
            template_id=data.get("template_id"),
            enabled=data["enabled"],
            automatic=data["automatic"],
        )
    elif name == "remove_template":
        await core.templates.async_remove(data["template_id"])
        result["template_id"] = data["template_id"]
    elif name == "create_job_from_template":
        result["job_id"] = await core.templates.async_create_job(data["template_id"])
    elif name == "reset_template_demand":
        await core.templates.async_reset_demand(data["template_id"])
        result["template_id"] = data["template_id"]
    elif name == "create_room":
        if (
            data.get("area_id")
            and ar.async_get(hass).async_get_area(data["area_id"]) is None
        ):
            raise ValidationError("unknown_area")
        room_id = await core.rooms.async_create(
            data["name"], area_id=data.get("area_id")
        )
    elif name == "update_room":
        values = normalize_room_patch(hass, data["configuration"])
        await core.rooms.async_update(
            data["room_id"], lambda room: apply_room_patch(room, values)
        )
    elif name == "disable_room":
        await core.rooms.async_disable(data["room_id"])
    elif name == "enable_room":
        await core.rooms.async_enable(data["room_id"])
    elif name == "release_room":
        result["grant_id"] = await core.rooms.async_grant(
            data["room_id"], ReleaseKind(data["kind"]), data.get("duration_seconds")
        )
    elif name == "revoke_room":
        await core.rooms.async_revoke(data["room_id"])
    elif name == "resolve_recovery":
        await core.async_resolve_recovery(
            data["robot_id"], confirm_stopped=data["confirm_stopped"]
        )
    else:
        controller = runtime.controller
        assert controller is not None
        entry = controller.entry
        if name == "remove_robot":
            require_idle_robot(entry, data["robot_id"])
            hass.config_entries.async_remove_subentry(entry, data["robot_id"])
        else:
            robot_id = configure_robot(
                hass, entry, data["configuration"], robot_id=robot_id
            )
    if room_id is not None:
        result["room_id"] = core.rooms.registry.resolve(room_id).room_id
    if robot_id is not None:
        result["robot_id"] = robot_id
    result["commit_id"] = core.state.commit_id
    return result


def present_template(template: JobTemplate) -> dict[str, Any]:
    """Return a reusable public intent without internal execution records."""
    intent = template.intent
    values: dict[str, Any] = {
        "areas": [target.area_id for target in intent.areas],
        "mode": intent.mode.value,
        "passes": intent.passes,
        "settings_policy": intent.settings_policy.value,
        "required_on": list(intent.required_on),
        "required_off": list(intent.required_off),
    }
    for key in ("name", "source", "reason", "note", "dedupe_key"):
        if (value := getattr(intent, key)) is not None:
            values[key] = value
    for key in ("vacuum_power", "mop_intensity", "mop_route"):
        if (value := getattr(intent.preferences, key)) is not None:
            values[key] = value.value
    return {
        "template_id": template.template_id,
        "name": template.name,
        "intent": values,
        "enabled": template.enabled,
        "automatic": template.automatic,
        "updated_at": template.updated_at.isoformat(),
        "suppressed_room_ids": list(template.demand_tokens),
    }


def setup_configuration_actions(hass: HomeAssistant) -> None:
    """Register bounded queries and admin-only mutations with stable error codes."""

    def handler_for(
        name: str, write: bool
    ) -> Callable[[ServiceCall], Coroutine[Any, Any, ServiceResponse | None]]:
        async def handle(call: ServiceCall) -> ServiceResponse | None:
            if write and call.context.user_id:
                user = await hass.auth.async_get_user(call.context.user_id)
                if user is None or not user.is_admin:
                    raise Unauthorized(context=call.context)
            try:
                with request_context(call.context):
                    result = (
                        await execute_configuration(hass, name, dict(call.data))
                        if write
                        else await async_query_configuration(
                            hass, name, dict(call.data)
                        )
                    )
            except OrchestratorError as err:
                raise service_error(err) from err
            return cast(ServiceResponse, result) if call.return_response else None

        return handle

    for name, schema in {**COMMANDS, **QUERIES}.items():
        write = name in COMMANDS
        hass.services.async_register(
            DOMAIN,
            name,
            handler_for(name, write),
            schema=schema,
            supports_response=SupportsResponse.OPTIONAL
            if write
            else SupportsResponse.ONLY,
        )
