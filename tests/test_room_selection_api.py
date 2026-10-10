"""Actions name rooms by area or room ID and robots by robot ID or vacuum."""

from __future__ import annotations

from datetime import timedelta

import probatio
import pytest
from homeassistant.components.vacuum.const import VacuumEntityFeature
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.vacuum_orchestrator.runtime import async_get_runtime
from tests.errors import raises_code
from tests.test_configuration_api import call, configured  # noqa: F401
from tests.test_job_holds_api import Card
from tests.test_validate_api import validate


async def _home(hass: HomeAssistant) -> dict[str, str]:
    """Kitchen and hall with areas, an attic without, one robot reaching both."""
    areas = ar.async_get(hass)
    kitchen, hall = areas.async_create("Kitchen"), areas.async_create("Hall")
    registry = er.async_get(hass)
    vacuum = registry.async_get_or_create("vacuum", "demo", "downstairs")
    registry.async_update_entity_options(
        vacuum.entity_id,
        "vacuum",
        {"area_mapping": {kitchen.id: ["16"], hall.id: ["17"]}},
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
    robot = (
        await call(
            hass,
            "add_robot",
            configuration={"robot_entity_id": vacuum.entity_id, "fixed_mode": "vacuum"},
        )
    )["robot_id"]
    await hass.async_block_till_done()
    rooms = async_get_runtime(hass).orchestrator.rooms
    return {
        "kitchen_area": kitchen.id,
        "hall_area": hall.id,
        "kitchen": rooms.room_id(kitchen.id),
        "hall": rooms.room_id(hall.id),
        "attic": (await call(hass, "create_room", name="Attic"))["room_id"],
        "vacuum": vacuum.entity_id,
        "robot": robot,
    }


@pytest.mark.usefixtures("configured")
async def test_rooms_are_released_and_revoked_by_area_or_room_id(
    hass: HomeAssistant,
) -> None:
    home = await _home(hass)
    commit = (await call(hass, "get_rooms"))["commit_id"]

    released = await call(
        hass,
        "release_room",
        areas=[home["kitchen_area"], home["hall_area"]],
        rooms=[home["attic"]],
        kind="timed",
        duration_seconds={"minutes": 15},
    )
    assert released["room_ids"] == [home["kitchen"], home["hall"], home["attic"]]
    assert (len(released["grant_ids"]), released["commit_id"]) == (3, commit + 1)
    rooms = {item["room_id"]: item for item in (await call(hass, "get_rooms"))["rooms"]}
    timed = rooms[home["attic"]]["release"]
    assert dt_util.parse_datetime(timed["expires_at"]) - dt_util.parse_datetime(
        timed["granted_at"]
    ) == timedelta(minutes=15)

    revoked = await call(
        hass, "revoke_room", areas=[home["kitchen_area"]], rooms=[home["attic"]]
    )
    assert (revoked["room_ids"], revoked["commit_id"]) == (
        [home["kitchen"], home["attic"]],
        commit + 2,
    )
    rooms = {item["room_id"]: item for item in (await call(hass, "get_rooms"))["rooms"]}
    assert [room for room, item in rooms.items() if item["released"]] == [home["hall"]]


@pytest.mark.usefixtures("configured")
async def test_a_release_error_names_the_area_or_room_and_changes_nothing(
    hass: HomeAssistant,
) -> None:
    home = await _home(hass)
    card = Card(hass)
    commit = (await call(hass, "get_rooms"))["commit_id"]

    for parameters, code, field in (
        (
            {"areas": [home["kitchen_area"]], "rooms": ["cellar"]},
            "unknown_room",
            "rooms.0",
        ),
        (
            {"areas": [home["kitchen_area"]], "rooms": [home["kitchen"]]},
            "duplicate_room",
            "rooms.0",
        ),
        (
            {"areas": [home["hall_area"]], "kind": "timed"},
            "release_duration_mismatch",
            "duration_seconds",
        ),
        ({}, "no_rooms", "areas"),
    ):
        result = await card.command(
            "release_room", **{"kind": "permanent", **parameters}
        )
        assert result == {"error": code}
        assert card.connection.translations[-1][3]["field"] == field
    assert await card.command("revoke_room", rooms=["cellar"]) == {
        "error": "unknown_room"
    }
    assert card.connection.translations[-1][3]["field"] == "rooms.0"
    assert (await call(hass, "get_rooms"))["commit_id"] == commit


@pytest.mark.usefixtures("configured")
async def test_jobs_take_areas_then_rooms_once_each_or_all_rooms(
    hass: HomeAssistant,
) -> None:
    home = await _home(hass)

    job = await call(
        hass,
        "create_job",
        areas=[home["hall_area"], home["kitchen_area"]],
        rooms=[home["attic"], home["kitchen"]],
    )
    detail = await call(hass, "get_job", job_id=job["job_id"])
    assert (detail["room_ids"], detail["all_rooms"]) == (
        [home["hall"], home["kitchen"], home["attic"]],
        False,
    )
    assert await validate(hass, "create_job", rooms=["cellar"]) == [
        ("rooms.0", "unknown_room")
    ]
    assert await validate(
        hass, "create_job", areas=[home["kitchen_area"]], all_rooms=True
    ) == [("all_rooms", "all_rooms_with_selection")]
    await call(hass, "disable_room", room_id=home["hall"])
    assert await validate(
        hass, "create_job", areas=[home["kitchen_area"], home["hall_area"]]
    ) == [("areas.1", "room_unavailable")]
    assert await validate(
        hass, "update_job", job_id=job["job_id"], rooms=[home["hall"]]
    ) == [("rooms.0", "room_unavailable")]


@pytest.mark.usefixtures("configured")
async def test_actions_name_a_robot_by_its_vacuum(hass: HomeAssistant) -> None:
    home = await _home(hass)

    await call(hass, "rename_robot", robot_id=home["vacuum"], name="Downstairs")
    (robot,) = (await call(hass, "get_robots"))["robots"]
    assert (robot["robot_id"], robot["name"]) == (home["robot"], "Downstairs")
    assert await validate(hass, "rename_robot", robot_id=home["vacuum"], name=" ") == [
        ("name", "invalid_robot_name")
    ]
    preview = await call(
        hass, "preview_job", areas=[home["kitchen_area"]], robot_id=home["vacuum"]
    )
    assert [item["robot_id"] for item in preview["robots"]] == [home["robot"]]

    await call(hass, "release_room", areas=[home["kitchen_area"]], kind="permanent")
    started: list[ServiceCall] = []

    async def clean(service: ServiceCall) -> None:
        started.append(service)

    hass.services.async_register("vacuum", "clean_area", clean)
    await call(
        hass,
        "create_job",
        areas=[home["kitchen_area"]],
        start=True,
        robot_id=home["vacuum"],
    )
    await hass.async_block_till_done()
    assert [item.data["entity_id"] for item in started] == [home["vacuum"]]
    with raises_code("unknown_robot"):
        await call(hass, "rename_robot", robot_id="vacuum.upstairs", name="Up")


@pytest.mark.usefixtures("configured")
async def test_queue_durations_accept_a_duration_or_seconds(
    hass: HomeAssistant,
) -> None:
    await call(
        hass,
        "configure_queue",
        grace_seconds={"minutes": 15},
        start_delay_seconds="00:00:30",
    )
    queue = await call(hass, "get_queue")
    assert (queue["queue_grace_seconds"], queue["start_delay_seconds"]) == (900, 30)
    await call(hass, "configure_queue", start_delay_seconds=12.5)
    assert (await call(hass, "get_queue"))["start_delay_seconds"] == 12.5
    with pytest.raises(probatio.Invalid):
        await call(hass, "configure_queue", start_delay_seconds={"minutes": 11})
