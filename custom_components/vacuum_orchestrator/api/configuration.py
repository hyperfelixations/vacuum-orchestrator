"""Authenticated room, robot and recovery actions sharing canonical validators."""

from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any, cast

import probatio
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
from ..application.preview import JobPreview, preview_draft
from ..configuration import configure_robot, require_idle_robot, robot_name
from ..const import API_VERSION, DOMAIN
from ..diagnostics import build_diagnostics
from ..domain.capabilities import CancelSemantics
from ..domain.errors import (
    ConflictError,
    OrchestratorError,
    ValidationError,
    located,
)
from ..domain.holds import HoldPurpose
from ..domain.intents import CleaningPreferences
from ..domain.maps import RobotMaps
from ..domain.permissions import robot_actions, room_actions, template_actions
from ..domain.queue import MAX_START_DELAY_SECONDS
from ..domain.releases import GrantRequest, ReleaseKind
from ..domain.templates import JobTemplate
from ..domain.types import ROUTE_LADDER, VACUUM_LADDER, WATER_LADDER, SettingsPolicy
from ..ha_context import request_context
from ..ports.entities import EntityReferences
from ..room_configuration import (
    ROOM_PATCH_SCHEMA,
    apply_room_patch,
    normalize_room_patch,
)
from ..runtime import async_get_runtime
from .errors import service_error
from .job_input import (
    ALL_ROOMS,
    ATTR_AREAS,
    ATTR_MODE,
    ATTR_MOP_INTENSITY,
    ATTR_MOP_ROUTE,
    ATTR_PASSES,
    ATTR_ROBOT_ID,
    ATTR_SETTINGS_POLICY,
    ATTR_VACUUM_POWER,
    CREATE_SCHEMA,
    INTENT_FIELDS,
    _mode,
    areas,
    intent_from_data,
    mop_route,
    vacuum_level,
    water_level,
)
from .presentation import (
    present_actions,
    present_job,
    present_job_defaults,
    present_job_view,
    present_references,
    present_settings,
    view_metadata,
)
from .room_presentation import present_room
from .telemetry import command_trace
from .validation import async_validate

if TYPE_CHECKING:
    from ..application.orchestrator import VacuumOrchestrator

PAGE = {
    probatio.Optional("offset", default=0): probatio.All(
        probatio.Coerce(int), probatio.Range(min=0)
    ),
    probatio.Optional("limit", default=50): probatio.All(
        probatio.Coerce(int), probatio.Range(min=1, max=100)
    ),
}
ROOM_ID: dict[Any, Any] = {probatio.Required("room_id"): cv.string}
ROBOT_ID: dict[Any, Any] = {probatio.Required("robot_id"): cv.string}
COMMANDS: dict[str, probatio.Schema | probatio.All[dict[str, Any]]] = {
    "configure_queue": probatio.All(
        probatio.Schema(
            {
                probatio.Optional("grace_seconds"): probatio.All(
                    probatio.Coerce(float), probatio.Range(min=0, max=86400)
                ),
                probatio.Optional("start_delay_seconds"): probatio.All(
                    probatio.Coerce(float),
                    probatio.Range(min=0, max=MAX_START_DELAY_SECONDS),
                ),
            }
        ),
        cv.has_at_least_one_key("grace_seconds", "start_delay_seconds"),
    ),
    "configure_job_defaults": probatio.Schema(
        {
            probatio.Optional("mode"): _mode,
            probatio.Optional("vacuum_power"): vacuum_level,
            probatio.Optional("mop_intensity"): water_level,
            probatio.Optional("mop_route"): mop_route,
            probatio.Optional("passes"): probatio.All(
                probatio.Coerce(int), probatio.Range(min=1, max=10)
            ),
            probatio.Optional("settings_policy"): probatio.Coerce(SettingsPolicy),
        }
    ),
    "save_template": probatio.Schema(
        {
            probatio.Optional("template_id"): cv.string,
            probatio.Required("name"): cv.string,
            probatio.Required("intent"): CREATE_SCHEMA,
            probatio.Optional("enabled", default=True): bool,
            probatio.Optional("automatic", default=False): bool,
        }
    ),
    "save_job_as_template": probatio.Schema(
        {
            probatio.Required("job_id"): cv.string,
            probatio.Required("name"): cv.string,
            probatio.Optional("automatic", default=False): bool,
        }
    ),
    "remove_template": probatio.Schema({probatio.Required("template_id"): cv.string}),
    "create_job_from_template": probatio.Schema(
        {probatio.Required("template_id"): cv.string}
    ),
    "reset_template_demand": probatio.Schema(
        {probatio.Required("template_id"): cv.string}
    ),
    "create_room": probatio.Schema(
        {probatio.Required("name"): cv.string, probatio.Optional("area_id"): cv.string}
    ),
    "update_room": probatio.Schema(
        {**ROOM_ID, probatio.Required("configuration"): ROOM_PATCH_SCHEMA}
    ),
    "disable_room": probatio.Schema(ROOM_ID),
    "enable_room": probatio.Schema(ROOM_ID),
    "release_room": probatio.Schema(
        {
            **ROOM_ID,
            probatio.Required("kind"): probatio.In(
                [kind.value for kind in ReleaseKind]
            ),
            probatio.Optional("duration_seconds"): probatio.All(
                probatio.Coerce(float), probatio.Range(min=0.001)
            ),
        }
    ),
    "revoke_room": probatio.Schema(ROOM_ID),
    "add_robot": probatio.Schema({probatio.Required("configuration"): dict}),
    "configure_robot": probatio.Schema(
        {**ROBOT_ID, probatio.Required("configuration"): dict}
    ),
    "remove_robot": probatio.Schema(ROBOT_ID),
    "rename_robot": probatio.Schema(
        {**ROBOT_ID, probatio.Optional("name"): probatio.Any(None, str)}
    ),
    "resolve_recovery": probatio.Schema(
        {**ROBOT_ID, probatio.Optional("confirm_stopped", default=False): bool}
    ),
}
# WebSocket only, never actions; see dev doc "Kartenbefehle".
CARD_COMMANDS: dict[str, probatio.Schema] = {
    "hold_job": probatio.Schema(
        {
            probatio.Required("job_id"): cv.string,
            probatio.Required("purpose"): probatio.Coerce(HoldPurpose),
        }
    ),
    "renew_job_hold": probatio.Schema({probatio.Required("hold_id"): cv.string}),
    "release_job_hold": probatio.Schema({probatio.Required("hold_id"): cv.string}),
    "release_rooms": probatio.Schema(
        {
            probatio.Required("grants"): [
                probatio.Schema(
                    {
                        probatio.Required("room"): cv.string,
                        probatio.Required("kind"): probatio.Coerce(ReleaseKind),
                        probatio.Optional("duration_seconds"): probatio.All(
                            probatio.Coerce(float), probatio.Range(min=0.001)
                        ),
                    }
                )
            ]
        }
    ),
    "revoke_rooms": probatio.Schema({probatio.Required("rooms"): [cv.string]}),
}
# Commands whose input `validate` checks; see dev doc "Validierung".
VALIDATED_COMMANDS = (
    "create_job",
    "update_job",
    "save_template",
    "create_room",
    "update_room",
    "release_rooms",
    "configure_queue",
    "configure_job_defaults",
    "add_robot",
    "configure_robot",
    "rename_robot",
)
QUERIES: dict[str, probatio.Schema] = {
    "validate": probatio.Schema(
        {
            probatio.Required("command"): probatio.In(VALIDATED_COMMANDS),
            probatio.Required("data"): dict,
        }
    ),
    "get_job_execution": probatio.Schema({probatio.Required("job_id"): cv.string}),
    "preview_job": probatio.Schema(
        {
            **{key: value for key, value in INTENT_FIELDS.items() if key != ATTR_AREAS},
            probatio.Optional(ATTR_AREAS): areas,
            probatio.Optional(ATTR_ROBOT_ID): cv.string,
        }
    ),
    "get_trace": probatio.Schema({**PAGE, probatio.Optional("job_id"): cv.string}),
    "get_history": probatio.Schema(PAGE),
    "get_diagnostics": probatio.Schema({}),
    "get_templates": probatio.Schema(PAGE),
    "get_rooms": probatio.Schema(PAGE),
    "get_room": probatio.Schema(ROOM_ID),
    "get_robots": probatio.Schema(PAGE),
    "get_robot_candidates": probatio.Schema(PAGE),
}


async def async_query_configuration(
    hass: HomeAssistant, name: str, data: dict[str, Any]
) -> dict[str, Any]:
    """Include live per-robot explanations without performing physical commands."""
    core = async_get_runtime(hass).orchestrator
    if name == "preview_job":
        return await _async_preview(core, data)
    if name == "validate":
        return await async_validate(
            hass,
            data["command"],
            data["data"],
            schemas={**COMMANDS, **CARD_COMMANDS},
            execute=_execute_configuration,
        ) | view_metadata(core)
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
                "readiness": present_job(job, core.entity_references, item.readiness)[
                    "readiness"
                ],
                "eligible": item.eligibility_reason is None
                and item.readiness.state.value == "ready",
                "eligibility_reason": item.eligibility_reason,
                "settings": present_settings(item.settings) if item.settings else [],
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
                "settings": present_settings(
                    core.state.assignments[attempt.attempt_id].settings
                ),
            }
            for attempt in core.state.attempts.values()
            if attempt.job_id == job.job_id
        ],
    } | view_metadata(core)


async def _async_preview(
    core: VacuumOrchestrator, data: dict[str, Any]
) -> dict[str, Any]:
    """Observe first; everything after it comes from one state, metadata too."""
    snapshot = await core.async_observed_snapshot() if ATTR_AREAS in data else None
    defaults = core.state.job_defaults
    intent, area_reason = None, None
    if ATTR_AREAS in data:
        try:
            intent = intent_from_data(
                data, core.active_room_ids, defaults, core.entity_references
            )
        except ConflictError as err:
            area_reason = err.code
    preview: JobPreview = preview_draft(
        core,
        snapshot,
        data.get(ATTR_MODE, defaults.mode),
        CleaningPreferences(
            data.get(ATTR_VACUUM_POWER),
            data.get(ATTR_MOP_INTENSITY),
            data.get(ATTR_MOP_ROUTE),
        ),
        intent,
        data.get(ATTR_ROBOT_ID),
    )
    reason = area_reason or preview.reason
    return {
        "api_version": API_VERSION,
        "mode": preview.mode.value,
        "passes": data.get(ATTR_PASSES, defaults.passes),
        "settings_policy": data.get(
            ATTR_SETTINGS_POLICY, defaults.settings_policy
        ).value,
        "settings": {
            name: {
                "requested": choice.requested,
                "options": [
                    {"value": value, "supported_by_all": everywhere}
                    for value, everywhere in choice.options
                ],
            }
            for name, choice in preview.settings.items()
        },
        "robots": [
            {
                "robot_id": item.robot_id,
                "operation": item.operation.value,
                "startable_now": item.reason is None,
                "reason": item.reason,
                "settings": present_settings(item.settings) if item.settings else [],
            }
            for item in preview.robots
        ],
        "unreachable_room_ids": list(preview.unreachable_room_ids),
        "startable_now": reason is None,
        "reason": reason,
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
    now = core.now()
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
            [
                present_template(value, core.entity_references)
                for value in core.state.templates.values()
            ],
            data,
            "templates",
        )
    if name == "get_room":
        room = core.rooms.registry.resolve(data["room_id"])
        waiting = core.jobs_awaiting_release()
        return {
            "api_version": API_VERSION,
            **present_room(
                room,
                now,
                waiting.get(room.room_id, ()),
                room_actions(core.state, room),
            ),
        }
    if name == "get_rooms":
        waiting = core.jobs_awaiting_release()
        return page(
            [
                present_room(
                    room,
                    now,
                    waiting.get(room.room_id, ()),
                    room_actions(core.state, room),
                )
                for room in core.rooms.registry.rooms.values()
            ],
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
        observation = core.latest_observations.get(robot_id)
        leased = any(
            lease.robot_id == robot_id for lease in core.state.robot_leases.values()
        )
        robots.append(
            {
                "robot_id": robot_id,
                **dict(
                    zip(
                        ("name", "name_source", "ha_name"),
                        robot_name(hass, subentry),
                        strict=True,
                    )
                ),
                "configuration": dict(subentry.data),
                "reach": []
                if adapter is None
                else [
                    {
                        "room_id": item.room_id,
                        "status": item.status.value,
                        "targets": list(item.targets),
                        "ignored": list(item.ignored),
                    }
                    for item in adapter.room_reach()
                ],
                **present_maps(adapter.maps() if adapter else RobotMaps()),
                "active": leased,
                "faults": []
                if observation is None
                else [
                    {
                        "code": fault.code,
                        "source": fault.source.value,
                        "operations": sorted(item.value for item in fault.operations),
                        "entity_id": fault.entity_id,
                        "needs_action": fault.needs_action,
                    }
                    for fault in observation.faults
                ],
                "actions": present_actions(
                    robot_actions(
                        leased=leased,
                        blocked=profile is not None
                        and profile.source_robot_id in core.state.blocked_robots,
                        returns=profile.capabilities.returns_to_dock
                        if profile
                        else None,
                        at_dock=observation.at_dock if observation else None,
                    )
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
                    "settings": {
                        name: [item.value for item in ladder if item in supported]
                        for name, ladder, supported in (
                            (
                                "vacuum_power",
                                VACUUM_LADDER,
                                profile.capabilities.vacuum_levels,
                            ),
                            (
                                "mop_intensity",
                                WATER_LADDER,
                                profile.capabilities.water_levels,
                            ),
                            (
                                "mop_route",
                                ROUTE_LADDER,
                                profile.capabilities.mop_routes,
                            ),
                        )
                    },
                    "unavailable_settings": sorted(
                        profile.capabilities.unavailable_settings
                    ),
                    "supports": {
                        "stop": profile.capabilities.cancel
                        is not CancelSemantics.UNSUPPORTED,
                        "return_to_dock": profile.capabilities.returns_to_dock,
                        "pause": False,
                    },
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
    if name == "hold_job":
        hold = await core.async_hold_job(data["job_id"], data["purpose"])
        result.update(
            hold_id=hold.hold_id,
            expires_at=hold.expires_at.isoformat(),
            job=present_job_view(
                core, core.state.jobs[hold.job_id], core.readiness_for_job(hold.job_id)
            ),
        )
    elif name == "renew_job_hold":
        renewed = await core.async_renew_job_hold(data["hold_id"])
        result["expires_at"] = renewed.expires_at.isoformat()
    elif name == "release_job_hold":
        await core.async_release_job_hold(data["hold_id"])
    elif name == "configure_queue":
        await core.runs.async_configure(
            grace_seconds=data.get("grace_seconds"),
            start_delay_seconds=data.get("start_delay_seconds"),
        )
        result["grace_seconds"] = core.state.queue_grace_seconds
        result["start_delay_seconds"] = core.state.start_delay_seconds
    elif name == "configure_job_defaults":
        await core.async_configure_job_defaults(data)
        result["job_defaults"] = present_job_defaults(core.state.job_defaults)
    elif name == "save_template":
        with located("intent"):
            intent = intent_from_data(
                data["intent"],
                core.active_room_ids,
                core.state.job_defaults,
                core.entity_references,
            )
        result["template_id"] = await core.templates.async_save(
            data["name"],
            intent,
            template_id=data.get("template_id"),
            enabled=data["enabled"],
            automatic=data["automatic"],
        )
    elif name == "save_job_as_template":
        result["template_id"] = await core.templates.async_save_job(
            data["job_id"], data["name"], automatic=data["automatic"]
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
            raise ValidationError("unknown_area", path=("area_id",))
        room_id = await core.rooms.async_create(
            data["name"], area_id=data.get("area_id")
        )
    elif name == "update_room":
        with located("configuration"):
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
    elif name == "release_rooms":
        requests = tuple(
            GrantRequest(item["room"], item["kind"], item.get("duration_seconds"))
            for item in data["grants"]
        )
        result["grant_ids"] = list(await core.rooms.async_grant_many(requests))
        result["room_ids"] = [
            core.rooms.registry.resolve(item.room).room_id for item in requests
        ]
    elif name == "revoke_rooms":
        result["room_ids"] = list(await core.rooms.async_revoke_many(data["rooms"]))
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
        elif name == "rename_robot":
            controller.rename_robot(data["robot_id"], data.get("name"))
        else:
            with located("configuration"):
                robot_id = configure_robot(
                    hass, entry, data["configuration"], robot_id=robot_id
                )
    if room_id is not None:
        result["room_id"] = core.rooms.registry.resolve(room_id).room_id
    if robot_id is not None:
        result["robot_id"] = robot_id
    result["commit_id"] = core.state.commit_id
    return result


def present_maps(maps: RobotMaps) -> dict[str, Any]:
    """Project a robot's maps; segment IDs are the vendor's own per map."""
    return {
        "map_image_entity_id": maps.current_image_ref,
        "maps": [
            {
                "map_id": item.map_id,
                "name": item.name,
                "current": item.current,
                "image_entity_id": item.image_ref,
                "segments": [
                    {"id": segment.segment_id, "name": segment.name}
                    for segment in item.segments
                ],
            }
            for item in maps.maps
        ],
        "maps_unavailable_reason": maps.unavailable_reason.value
        if maps.unavailable_reason
        else None,
    }


def present_template(
    template: JobTemplate, references: EntityReferences
) -> dict[str, Any]:
    """Return a reusable public intent without internal execution records."""
    intent = template.intent
    values: dict[str, Any] = {
        "areas": ALL_ROOMS
        if intent.all_rooms
        else [target.area_id for target in intent.areas],
        "mode": intent.mode.value,
        "passes": intent.passes,
        "settings_policy": intent.settings_policy.value,
        "required_on": present_references(intent.required_on, references),
        "required_off": present_references(intent.required_off, references),
    }
    for key in ("name", "reason", "note", "dedupe_key"):
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
        "actions": present_actions(template_actions(template)),
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
