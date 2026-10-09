"""`validate` checks a command's input exactly like the command, without effect."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.components.vacuum.const import VacuumEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.vacuum_orchestrator.api.websocket import (
    websocket_configuration_get,
)
from custom_components.vacuum_orchestrator.runtime import async_get_runtime
from tests.test_configuration_api import call, configured  # noqa: F401
from tests.test_job_holds_api import Card
from tests.test_websocket import Connection


async def validate(hass: HomeAssistant, command: str, **data: Any) -> list[Any]:
    """Return the field errors; an unchanged state proves nothing was applied."""
    core = async_get_runtime(hass).orchestrator
    commit, sequence = core.state.commit_id, core.runtime_sequence
    result = await call(hass, "validate", command=command, data=data)
    assert (core.state.commit_id, core.runtime_sequence) == (commit, sequence)
    assert result["valid"] is (not result["errors"])
    return [
        (".".join(map(str, item["path"])), item["code"]) for item in result["errors"]
    ]


@pytest.mark.usefixtures("configured")
async def test_a_job_draft_is_checked_like_create_job(hass: HomeAssistant) -> None:
    room = (await call(hass, "create_room", name="Office"))["room_id"]

    assert await validate(hass, "create_job", areas=[room]) == []
    assert async_get_runtime(hass).orchestrator.state.jobs == {}
    # Given fields in input order, then the missing ones.
    assert await validate(hass, "create_job", passes=11, colour="red") == [
        ("passes", "out_of_range"),
        ("colour", "unknown_field"),
        ("areas", "required_field"),
    ]
    assert await validate(hass, "create_job", areas=[room], mode="sweep") == [
        ("mode", "invalid_value")
    ]
    assert await validate(hass, "create_job", areas=[room], name=" ") == [
        ("name", "empty_name")
    ]
    assert await validate(hass, "create_job", areas=[room, "attic"]) == [
        ("areas.1", "unknown_room")
    ]
    await call(hass, "create_job", areas=[room], dedupe_key="daily")
    assert await validate(hass, "create_job", areas=[room], dedupe_key="daily") == [
        ("dedupe_key", "dedupe_key_already_queued")
    ]


@pytest.mark.usefixtures("configured")
async def test_an_edit_is_checked_with_the_hold_that_protects_it(
    hass: HomeAssistant,
) -> None:
    room = (await call(hass, "create_room", name="Office"))["room_id"]
    job = (await call(hass, "create_job", areas=[room]))["job_id"]
    held = await Card(hass).command("hold_job", job_id=job, purpose="edit")

    assert await validate(hass, "update_job", job_id=job, note="Later") == [
        ("", "job_held")
    ]
    assert (
        await validate(
            hass, "update_job", job_id=job, note="Later", hold_id=held["hold_id"]
        )
        == []
    )
    assert (
        async_get_runtime(hass).orchestrator.state.job_holds[job].hold_id
        == (held["hold_id"])
    )
    assert await validate(
        hass, "update_job", job_id=job, reason=" ", hold_id=held["hold_id"]
    ) == [("reason", "empty_reason")]


@pytest.mark.usefixtures("configured")
async def test_rooms_releases_and_settings_are_checked_without_effect(
    hass: HomeAssistant,
) -> None:
    room = (await call(hass, "create_room", name="Office"))["room_id"]
    before = async_get_runtime(hass).orchestrator.state

    assert await validate(hass, "create_room", name="Hall") == []
    assert await validate(hass, "create_room", name="Hall", area_id="nowhere") == [
        ("area_id", "unknown_area")
    ]
    assert await validate(
        hass, "update_room", room_id=room, configuration={"area_id": "nowhere"}
    ) == [("configuration.area_id", "unknown_area")]
    assert (
        await validate(
            hass, "release_rooms", grants=[{"room": room, "kind": "permanent"}]
        )
        == []
    )
    assert await validate(
        hass,
        "release_rooms",
        grants=[{"room": room, "kind": "timed"}, {"room": room, "kind": "once"}],
    ) == [("grants.0.duration_seconds", "release_duration_mismatch")]
    assert await validate(hass, "configure_queue", start_delay_seconds=601) == [
        ("start_delay_seconds", "out_of_range")
    ]
    assert await validate(hass, "configure_queue") == [("", "required_field")]
    assert await validate(hass, "configure_job_defaults", passes=3) == []
    assert await validate(
        hass, "save_template", name="Daily", intent={"areas": [room], "note": " "}
    ) == [("intent.note", "empty_note")]
    assert async_get_runtime(hass).orchestrator.state == before


@pytest.mark.usefixtures("configured")
async def test_robots_are_checked_without_touching_their_entries(
    hass: HomeAssistant,
) -> None:
    vacuum = er.async_get(hass).async_get_or_create("vacuum", "demo", "check")
    hass.states.async_set(
        vacuum.entity_id,
        "docked",
        {"supported_features": int(VacuumEntityFeature.CLEAN_AREA)},
    )
    entry = async_get_runtime(hass).controller.entry
    assert (
        await validate(
            hass, "add_robot", configuration={"robot_entity_id": vacuum.entity_id}
        )
        == []
    )
    assert entry.subentries == {}
    robot = (
        await call(
            hass,
            "add_robot",
            configuration={"robot_entity_id": vacuum.entity_id, "fixed_mode": "vacuum"},
        )
    )["robot_id"]
    data = dict(entry.subentries[robot].data)

    assert await validate(
        hass, "configure_robot", robot_id=robot, configuration={"preference": 500}
    ) == [("configuration.preference", "invalid_robot_preference")]
    assert await validate(hass, "rename_robot", robot_id=robot, name=" ") == [
        ("name", "invalid_robot_name")
    ]
    assert await validate(hass, "rename_robot", robot_id=robot, name="Robi") == []
    assert dict(entry.subentries[robot].data) == data


@pytest.mark.usefixtures("configured")
async def test_only_commands_with_fields_can_be_validated(hass: HomeAssistant) -> None:
    connection = Connection()
    websocket_configuration_get(
        hass,
        connection,  # type: ignore[arg-type]
        {
            "id": 1,
            "query": "validate",
            "parameters": {"command": "end_queue", "data": {}},
        },
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert connection.errors[0][1] == "invalid_parameters"
    assert connection.translations[0][3]["field"] == "command"
