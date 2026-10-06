"""Contract tests for room, robot and recovery configuration boundaries."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import voluptuous as vol
from homeassistant.components.vacuum.const import VacuumEntityFeature
from homeassistant.core import Context, ServiceCall
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vacuum_orchestrator.api.actions import async_setup_actions
from custom_components.vacuum_orchestrator.api.websocket import (
    websocket_configuration_command,
    websocket_configuration_get,
)
from custom_components.vacuum_orchestrator.configuration_values import (
    normalize_requirements,
)
from custom_components.vacuum_orchestrator.const import CONF_INSTALLATION_ID, DOMAIN
from custom_components.vacuum_orchestrator.domain.errors import ValidationError
from custom_components.vacuum_orchestrator.domain.releases import ReleaseKind
from custom_components.vacuum_orchestrator.domain.requirements import StateRequirement
from custom_components.vacuum_orchestrator.domain.types import (
    OperationKind,
    VacuumLevel,
)
from custom_components.vacuum_orchestrator.room_configuration import (
    normalize_room_patch,
)
from custom_components.vacuum_orchestrator.runtime import (
    async_get_runtime,
    async_setup_orchestrator,
    async_unload_orchestrator,
)
from tests.application.test_orchestrator import RecordingAdapter, RecordingBackend
from tests.errors import raises_code
from tests.test_runtime import MemoryBackend
from tests.test_websocket import Connection


@pytest.fixture
async def configured(hass, monkeypatch):
    MemoryBackend.data = None
    monkeypatch.setattr(
        "custom_components.vacuum_orchestrator.runtime.HomeAssistantSnapshotBackend",
        MemoryBackend,
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_INSTALLATION_ID: DOMAIN, "auto_discover_robots": False},
    )
    entry.add_to_hass(hass)
    await async_setup_actions(hass)
    await async_setup_orchestrator(hass, entry)
    await hass.async_block_till_done()
    yield entry
    await async_unload_orchestrator(hass, entry)


async def call(hass, action, **data):
    return await hass.services.async_call(
        DOMAIN, action, data, blocking=True, return_response=True
    )


async def test_room_actions_preserve_runtime_facts_and_stable_conditions(
    hass, configured
):
    sensor = er.async_get(hass).async_get_or_create("binary_sensor", "demo", "door")
    room = (await call(hass, "create_room", name="Study"))["room_id"]
    initial = await call(hass, "get_room", room_id=room)
    assert not initial["released"]
    assert initial["due"]["vacuum"]["state"] == "disabled"
    await call(
        hass,
        "update_room",
        room_id=room,
        configuration={
            "name": "Office",
            "due_policy": {
                "basis": "occupied",
                "occupancy_entity_id": sensor.entity_id,
                "vacuum_seconds": 7200,
            },
            "requirements": [
                {"entity_id": sensor.entity_id, "accepted_states": ["on"]}
            ],
            "bindings": [{"robot_id": "robot", "target_ids": ["16"], "map_id": "0"}],
        },
    )
    detail = await call(hass, "get_room", room_id=room)
    assert detail["name"] == "Office" and not detail["follow_area_name"]
    assert detail["due_policy"]["occupancy_entity_registry_id"] == sensor.id
    assert detail["requirements"][0]["entity_registry_id"] == sensor.id
    assert detail["due"]["vacuum"]["state"] == "due"
    await call(
        hass,
        "update_room",
        room_id=room,
        configuration={"due_policy": {"mop_seconds": 3600}, "area_id": None},
    )
    assert (await call(hass, "get_room", room_id=room))["due_policy"][
        "vacuum_seconds"
    ] == 7200
    grant = await call(
        hass, "release_room", room_id=room, kind="timed", duration_seconds=7200
    )
    assert grant["grant_id"]
    assert (await call(hass, "get_room", room_id=room))["released"]
    revoked = await call(hass, "revoke_room", room_id=room)
    assert revoked == {
        "api_version": 3,
        "commit_id": async_get_runtime(hass).orchestrator.state.commit_id,
        "room_id": room,
    }
    assert revoked["commit_id"] > grant["commit_id"]
    assert not (await call(hass, "get_room", room_id=room))["released"]
    await call(hass, "disable_room", room_id=room)
    assert not (await call(hass, "get_room", room_id=room))["enabled"]
    enabled = await call(hass, "enable_room", room_id=room)
    assert enabled["room_id"] == room
    assert (await call(hass, "get_room", room_id=room))["enabled"]
    page = await call(hass, "get_rooms", offset=1, limit=1)
    assert page["total"] == 1 and not page["rooms"]
    core = async_get_runtime(hass).orchestrator
    assert (page["commit_id"], page["runtime_id"], page["runtime_sequence"]) == (
        core.state.commit_id,
        core.runtime_id,
        core.runtime_sequence,
    )
    with pytest.raises(vol.Invalid):
        await call(
            hass, "update_room", room_id=room, configuration={"last_cleaning": {}}
        )
    with raises_code("release_duration_mismatch"):
        await call(hass, "release_room", room_id=room, kind="once", duration_seconds=10)
    with raises_code("unknown_room"):
        await call(hass, "get_room", room_id="missing")


async def test_robot_configuration_actions_share_validation_and_idle_guard(
    hass, configured, monkeypatch
):
    registry = er.async_get(hass)
    vacuum = registry.async_get_or_create("vacuum", "demo", "robot")
    sensor = registry.async_get_or_create("binary_sensor", "demo", "water")
    hass.states.async_set(
        vacuum.entity_id, "docked", {"fan_speed_list": ["quiet", "max", "off"]}
    )
    candidates = await call(hass, "get_robot_candidates")
    assert candidates["candidates"][0]["registry_id"] == vacuum.id
    robot = (
        await call(
            hass,
            "add_robot",
            configuration={"robot_entity_id": vacuum.entity_id, "fixed_mode": "vacuum"},
        )
    )["robot_id"]
    await hass.async_block_till_done()
    await call(
        hass,
        "configure_robot",
        robot_id=robot,
        configuration={
            "minimum_battery": 30,
            "vacuum_levels": {"maximum": "max", "low": "quiet", "off": "off"},
            "requirements": [
                {
                    "entity_id": sensor.entity_id,
                    "accepted_states": ["off"],
                    "operation": "mop",
                }
            ],
        },
    )
    await hass.async_block_till_done()
    projection = (await call(hass, "get_robots"))["robots"][0]
    assert projection["configuration"]["minimum_battery"] == 30
    assert projection["capabilities"]["maximum_passes"] == 1
    assert projection["capabilities"]["settings"] == {
        "vacuum_power": ["low", "maximum"],
        "mop_intensity": [],
        "mop_route": [],
    }
    assert projection["capabilities"]["unavailable_settings"] == []
    assert projection["capabilities"]["supports"] == {
        "stop": False,
        "return_to_dock": False,
        "pause": False,
    }
    assert (
        configured.runtime_data.orchestrator.adapters[robot]
        .profile.requirements[0]
        .entity_registry_id
        == sensor.id
    )
    with raises_code("already_configured"):
        await call(
            hass, "add_robot", configuration={"robot_entity_id": vacuum.entity_id}
        )
    await call(
        hass, "configure_robot", robot_id=robot, configuration={"enabled": False}
    )
    await hass.async_block_till_done()
    assert (await call(hass, "get_robots"))["robots"][0]["capabilities"] is None
    await call(hass, "remove_robot", robot_id=robot)
    await hass.async_block_till_done()
    assert not (await call(hass, "get_robots"))["robots"]
    resolve = AsyncMock()
    monkeypatch.setattr(
        configured.runtime_data.orchestrator, "async_resolve_recovery", resolve
    )
    await call(hass, "resolve_recovery", robot_id="retained", confirm_stopped=True)
    resolve.assert_awaited_once_with("retained", confirm_stopped=True)


async def test_configuration_actions_enforce_admin_and_return_optional_response(
    hass, configured, monkeypatch
):
    monkeypatch.setattr(
        hass.auth,
        "async_get_user",
        AsyncMock(return_value=SimpleNamespace(is_admin=False)),
    )
    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN,
            "create_room",
            {"name": "Room"},
            blocking=True,
            context=Context(user_id="user"),
        )
    monkeypatch.setattr(
        hass.auth,
        "async_get_user",
        AsyncMock(return_value=SimpleNamespace(is_admin=True)),
    )
    assert (
        await hass.services.async_call(
            DOMAIN,
            "create_room",
            {"name": "Room"},
            blocking=True,
            context=Context(user_id="admin"),
        )
        is None
    )
    assert (await call(hass, "get_rooms"))["total"] == 1


async def test_configuration_websocket_uses_action_schemas(hass, configured):
    connection = Connection()
    connection.user = SimpleNamespace(is_admin=True)
    websocket_configuration_command(
        hass,
        connection,
        {"id": 1, "command": "create_room", "parameters": {"name": "Room"}},
    )
    await hass.async_block_till_done()
    assert connection.results[0][1]["room_id"]
    websocket_configuration_get(
        hass, connection, {"id": 2, "query": "get_rooms", "parameters": {}}
    )
    websocket_configuration_get(
        hass, connection, {"id": 3, "query": "get_rooms", "parameters": {"limit": 101}}
    )
    websocket_configuration_get(
        hass,
        connection,
        {"id": 4, "query": "get_room", "parameters": {"room_id": "absent"}},
    )
    websocket_configuration_command(
        hass,
        connection,
        {
            "id": 5,
            "command": "release_room",
            "parameters": {"room_id": "absent", "kind": "permanent"},
        },
    )
    websocket_configuration_command(
        hass, connection, {"id": 6, "command": "create_room", "parameters": {}}
    )
    await hass.async_block_till_done()
    assert connection.results[1][1]["total"] == 1
    assert {item[1] for item in connection.errors} == {
        "invalid_parameters",
        "unknown_room",
    }
    connection.user.is_admin = False
    with pytest.raises(Unauthorized):
        websocket_configuration_command(hass, connection, {"id": 7})


@pytest.mark.parametrize(
    "value",
    [
        True,
        [{"entity_id": "bad"}],
        [{"entity_id": "sensor.value", "entity_registry_id": "missing"}],
        [{"entity_id": "sensor.value", "accepted_states": ["unknown"]}],
    ],
)
def test_configuration_rejects_invalid_requirements(hass, value):
    with pytest.raises(ValidationError):
        normalize_requirements(hass, value)


def test_room_patch_rejects_runtime_fields(hass):
    with pytest.raises(ValidationError, match="invalid_room_configuration"):
        normalize_room_patch(hass, {"occupancy": {}})


async def test_template_actions_instantiate_and_preserve_intent_snapshots(
    hass, configured
):
    room_id = (await call(hass, "create_room", name="Room"))["room_id"]
    saved = await call(
        hass,
        "save_template",
        name="Routine",
        intent={
            "areas": [room_id],
            "mode": "vacuum",
            "name": "Clean",
            "vacuum_power": "high",
        },
    )
    template_id = saved["template_id"]
    templates = await call(hass, "get_templates")
    assert templates["templates"][0]["intent"]["vacuum_power"] == "high"
    assert not templates["templates"][0]["automatic"]
    created = await call(hass, "create_job_from_template", template_id=template_id)
    assert (
        configured.runtime_data.orchestrator.state.jobs[created["job_id"]].intent.name
        == "Clean"
    )
    await call(hass, "reset_template_demand", template_id=template_id)
    await call(hass, "remove_template", template_id=template_id)
    assert not (await call(hass, "get_templates"))["templates"]


async def test_job_defaults_are_copied_into_jobs_and_templates(hass, configured):
    core = configured.runtime_data.orchestrator
    room_id = (await call(hass, "create_room", name="Room"))["room_id"]
    assert (await call(hass, "get_queue"))["job_defaults"] == {
        "mode": "vacuum",
        "vacuum_power": "standard",
        "mop_intensity": "medium",
        "mop_route": "standard",
        "passes": 1,
        "settings_policy": "best_effort",
        "configured": False,
    }
    before = core.state.commit_id
    configured_defaults = await call(
        hass,
        "configure_job_defaults",
        mode="mop",
        mop_intensity="high",
        passes=2,
        settings_policy="strict",
    )
    assert configured_defaults["commit_id"] == before + 1
    assert configured_defaults["job_defaults"] == {
        "mode": "mop",
        "vacuum_power": "standard",
        "mop_intensity": "high",
        "mop_route": "standard",
        "passes": 2,
        "settings_policy": "strict",
        "configured": True,
    }
    repeated = await call(hass, "configure_job_defaults", mode="mop")
    assert repeated["commit_id"] == before + 1

    plain = await call(hass, "create_job", areas=[room_id])
    detail = await call(hass, "get_job", job_id=plain["job_id"])
    assert (
        detail["mode"],
        detail["vacuum_power"],
        detail["mop_intensity"],
        detail["mop_route"],
        detail["passes"],
        detail["settings_policy"],
    ) == ("mop", None, "high", "standard", 2, "strict")
    explicit = await call(
        hass, "create_job", areas=[room_id], mode="vacuum", vacuum_power="maximum_plus"
    )
    detail = await call(hass, "get_job", job_id=explicit["job_id"])
    assert (detail["vacuum_power"], detail["mop_intensity"]) == ("maximum_plus", None)

    await call(
        hass,
        "save_template",
        name="Both",
        intent={"areas": [room_id], "mode": "vacuum_and_mop"},
    )
    intent = (await call(hass, "get_templates"))["templates"][0]["intent"]
    assert (
        intent["vacuum_power"],
        intent["mop_intensity"],
        intent["mop_route"],
        intent["passes"],
    ) == ("standard", "high", "standard", 2)
    for invalid in ({"vacuum_power": "medium"}, {"mop_intensity": "standard"}):
        with pytest.raises(vol.Invalid):
            await call(hass, "configure_job_defaults", **invalid)


async def test_preview_and_robot_capabilities_use_the_ladders(hass, configured):
    await configured.runtime_data.controller.scheduler.async_close()
    core = configured.runtime_data.orchestrator
    room = (await call(hass, "create_room", name="Office"))["room_id"]
    adapter = RecordingAdapter(RecordingBackend(), "robot", targets=(room,))
    adapter._profile = replace(
        adapter.profile,
        capabilities=replace(
            adapter.profile.capabilities,
            vacuum_levels=frozenset({VacuumLevel.MAXIMUM, VacuumLevel.LOW}),
        ),
    )
    await core.async_replace_adapters({"robot": adapter})

    draft = await call(hass, "preview_job", mode="vacuum")
    assert draft["startable_now"] is False and draft["reason"] == "job_requires_area"
    assert draft["settings"] == {
        "vacuum_power": {
            "initial": "low",
            "options": [
                {"value": "low", "supported_by_all": True},
                {"value": "maximum", "supported_by_all": True},
            ],
        }
    }
    before = core.state.commit_id
    preview = await call(
        hass, "preview_job", areas=[room], vacuum_power="high", robot_id="robot"
    )
    assert core.state.commit_id == before
    assert (preview["mode"], preview["passes"], preview["settings_policy"]) == (
        "vacuum",
        1,
        "best_effort",
    )
    assert preview["settings"]["vacuum_power"]["initial"] == "maximum"
    assert preview["reason"] == "job_blocked" and not preview["startable_now"]
    assert preview["robots"] == [
        {
            "robot_id": "robot",
            "operation": "vacuum",
            "startable_now": False,
            "reason": "room_not_released",
            "settings": [
                {"name": "vacuum_power", "requested": "high", "applied": "maximum"}
            ],
        }
    ]
    await core.rooms.async_grant(room, ReleaseKind.PERMANENT)
    ready = await call(hass, "preview_job", areas="all")
    assert ready["startable_now"] and ready["reason"] is None
    await call(hass, "disable_room", room_id=room)
    assert (await call(hass, "preview_job", areas="all"))["reason"] == (
        "no_eligible_rooms"
    )
    connection = Connection()
    websocket_configuration_get(
        hass,
        connection,
        {"id": 1, "query": "preview_job", "parameters": {"mode": "mop"}},
    )
    await hass.async_block_till_done()
    assert set(connection.results[0][1]["settings"]) == {"mop_intensity", "mop_route"}


async def test_cancel_and_return_actions_expose_the_return_choice(hass, configured):
    await configured.runtime_data.controller.scheduler.async_close()
    core = configured.runtime_data.orchestrator
    room = (await call(hass, "create_room", name="Office"))["room_id"]
    adapter = RecordingAdapter(RecordingBackend(), "robot", targets=(room,))
    # The runtime store is not the fake's backend; physical calls stay recorded.
    adapter._persisted_attempt_state = lambda: None
    await core.async_replace_adapters({"robot": adapter})
    await core.rooms.async_grant(room, ReleaseKind.PERMANENT)

    returned = await call(hass, "return_robot", robot_id="robot")
    assert returned["robot_id"] == "robot" and adapter.return_count == 1
    job = (await call(hass, "create_job", areas=[room]))["job_id"]
    await call(hass, "start_job", job_id=job)
    assert (await call(hass, "get_job", job_id=job))["after_cancel"] is None
    await call(hass, "cancel_job", job_id=job, after_cancel="return_to_dock")
    detail = await call(hass, "get_job", job_id=job)
    assert (detail["state"], detail["after_cancel"]) == ("canceling", "return_to_dock")
    assert adapter.cancel_returns == [True]
    with raises_code("robot_already_executing"):
        await call(hass, "return_robot", robot_id="robot")
    second = (await call(hass, "create_job", areas=[room]))["job_id"]
    await call(hass, "run_queue")
    ended = await call(hass, "end_queue")
    assert ended["mode"] == "paused"
    assert (await call(hass, "get_queue"))["queue_run"]["ending"] is True
    assert (await call(hass, "get_job", job_id=second))["state"] == "queued"
    ended = await call(
        hass, "end_queue", running_jobs="cancel", after_cancel="return_to_dock"
    )
    assert ended["mode"] == "idle"
    assert (await call(hass, "get_queue"))["queue_run"]["ending"] is False
    with pytest.raises(vol.Invalid):
        await call(hass, "end_queue", running_jobs="abort")
    with pytest.raises(vol.Invalid):
        await call(hass, "cancel_job", job_id=job, after_cancel="home")


async def test_job_request_origin_survives_queueing_and_reaches_physical_calls(
    hass, configured
):
    area = ar.async_get(hass).async_create("Room")
    registry = er.async_get(hass)
    vacuum = registry.async_get_or_create("vacuum", "demo", "robot")
    registry.async_update_entity_options(
        vacuum.entity_id, "vacuum", {"area_mapping": {area.id: ["16"]}}
    )
    hass.states.async_set(
        vacuum.entity_id,
        "docked",
        {
            "supported_features": int(
                VacuumEntityFeature.CLEAN_AREA | VacuumEntityFeature.STOP
            )
        },
    )
    await call(
        hass,
        "add_robot",
        configuration={"robot_entity_id": vacuum.entity_id, "fixed_mode": "vacuum"},
    )
    await hass.async_block_till_done()
    room_id = configured.runtime_data.orchestrator.rooms.registry.resolve(
        area.id
    ).room_id
    await call(hass, "release_room", room_id=room_id, kind="permanent")
    observed = []

    async def clean(call: ServiceCall):
        observed.append(call.context)

    hass.services.async_register("vacuum", "clean_area", clean)
    origin = Context(id="origin", parent_id="parent")
    created = await hass.services.async_call(
        DOMAIN,
        "create_job",
        {"areas": [area.id], "mode": "vacuum"},
        context=origin,
        blocking=True,
        return_response=True,
    )
    await call(hass, "run_queue")
    await hass.async_block_till_done()
    assert observed, configured.runtime_data.orchestrator.trace.snapshot()
    assert observed[0].id == "origin" and observed[0].parent_id == "parent"
    assert (
        configured.runtime_data.orchestrator.state.jobs[
            created["job_id"]
        ].origin.context_id
        == "origin"
    )


async def test_public_configuration_rejects_mistyped_fields_and_missing_area(
    hass, configured
):
    vacuum = er.async_get(hass).async_get_or_create("vacuum", "demo", "guarded")
    with raises_code("unknown_robot_configuration_field"):
        await call(
            hass,
            "add_robot",
            configuration={
                "robot_entity_id": vacuum.entity_id,
                "mimimum_battery": 20,
            },
        )
    with raises_code("unknown_area"):
        await call(hass, "create_room", name="Office", area_id="missing")
    room = (await call(hass, "create_room", name="Office"))["room_id"]
    with raises_code("unknown_area"):
        await call(
            hass, "update_room", room_id=room, configuration={"area_id": "missing"}
        )


async def test_execution_query_explains_scoped_blockers_and_resolved_settings(
    hass, configured
):
    await configured.runtime_data.controller.scheduler.async_close()
    core = configured.runtime_data.orchestrator
    room = (await call(hass, "create_room", name="Office"))["room_id"]
    adapter = RecordingAdapter(RecordingBackend(), "robot", targets=(room,))
    await core.async_replace_adapters({"robot": adapter})
    door = er.async_get(hass).async_get_or_create(
        "binary_sensor", "demo", "explain_door"
    )
    hass.states.async_set(door.entity_id, "off")
    await core.rooms.async_update(
        room,
        lambda value: replace(
            value,
            requirements=(
                StateRequirement(
                    door.entity_id, robot_id="robot", operation=OperationKind.MOP
                ),
            ),
        ),
    )
    job = (
        await call(
            hass,
            "create_job",
            areas=[room],
            mode="vacuum_then_mop",
            vacuum_power="high",
        )
    )["job_id"]
    await core.rooms.async_grant(room, ReleaseKind.PERMANENT)
    before = core.state
    result = await call(hass, "get_job_execution", job_id=job)
    assert core.state is before and not adapter.dispatches
    assert result["commit_id"] == before.commit_id
    vacuum, mop = result["robots"]
    assert vacuum["eligible"] and vacuum["settings"] == [
        {"name": "vacuum_power", "requested": "high", "applied": None}
    ]
    assert not mop["eligible"]
    condition = mop["readiness"]["requirements"][0]
    assert condition["room_id"] == room and condition["robot_id"] == "robot"
    assert condition["operation"] == "mop"
    assert condition["reason"] == "requirement_not_satisfied"
    adapter._profile = replace(
        adapter.profile, allowed_operations=frozenset({OperationKind.VACUUM})
    )
    result = await call(hass, "get_job_execution", job_id=job)
    assert result["robots"][1]["eligibility_reason"] == "unsupported_operation"
    connection = Connection()
    websocket_configuration_get(
        hass,
        connection,
        {
            "id": 18,
            "query": "get_job_execution",
            "parameters": {"job_id": job},
        },
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert connection.results[0][1] == result
    with raises_code("unknown_job"):
        await call(hass, "get_job_execution", job_id="missing")
