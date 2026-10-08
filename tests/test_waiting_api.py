"""Jobs and rooms expose VOI's waiting reasons; clients only format them."""

import pytest
from homeassistant.core import HomeAssistant

from tests.test_configuration_api import call, configured  # noqa: F401


@pytest.mark.usefixtures("configured")
async def test_jobs_name_their_blockers_and_rooms_their_waiting_jobs(
    hass: HomeAssistant,
) -> None:
    room = (await call(hass, "create_room", name="Office"))["room_id"]
    job = (await call(hass, "create_job", areas=[room]))["job_id"]

    detail = await call(hass, "get_job", job_id=job)
    waiting = detail["waiting"]
    assert (waiting["code"], waiting["room_ids"], waiting["until"]) == (
        "room_not_released",
        [room],
        None,
    )
    assert [item["code"] for item in waiting["blockers"]] == [
        "room_not_released",
        "no_robot_configured",
        "queue_idle",
        "start_delayed",
    ]
    assert waiting["blockers"][-1]["until"] == detail["start_after"]
    assert waiting["blockers"][0] == {
        "code": "room_not_released",
        "until": None,
        "room_ids": [room],
        "entity_ids": [],
        "robot_ids": [],
    }
    assert (await call(hass, "get_queue"))["jobs"][0]["waiting"] == waiting
    assert (await call(hass, "get_room", room_id=room))["waiting_job_ids"] == [job]
    rooms = (await call(hass, "get_rooms"))["rooms"]
    assert [item["waiting_job_ids"] for item in rooms] == [[job]]

    await call(hass, "release_room", room_id=room, kind="permanent")
    assert (await call(hass, "get_room", room_id=room))["waiting_job_ids"] == []
    released = (await call(hass, "get_job", job_id=job))["waiting"]
    assert released["code"] == "no_robot_configured"
