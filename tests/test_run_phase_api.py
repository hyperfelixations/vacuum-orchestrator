"""The queue read model names the run phase and when standby ends."""

from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from tests.test_configuration_api import call, configured  # noqa: F401


@pytest.mark.usefixtures("configured")
async def test_an_empty_running_queue_is_on_standby_until_its_grace_ends(
    hass: HomeAssistant,
) -> None:
    queue = await call(hass, "get_queue")
    assert queue["queue_run"] is None

    await call(hass, "configure_queue", grace_seconds=600)
    await call(hass, "run_queue")
    await hass.async_block_till_done()
    run = (await call(hass, "get_queue"))["queue_run"]
    assert run["phase"] == "standby"
    assert run["ends_at"] == run["deadline"]
    assert dt_util.parse_datetime(run["ends_at"]) - dt_util.parse_datetime(
        run["idle_since"]
    ) == timedelta(seconds=600)

    room = (await call(hass, "create_room", name="Office"))["room_id"]
    await call(hass, "release_room", room_id=room, kind="permanent")
    job = (await call(hass, "create_job", areas=[room]))["job_id"]
    assert (await call(hass, "get_job", job_id=job))["waiting"]["code"] == (
        "no_robot_configured"
    )
    blocked = (await call(hass, "get_queue"))["queue_run"]
    assert (blocked["phase"], blocked["ends_at"]) == ("standby", run["ends_at"])

    await call(hass, "pause_queue")
    paused = (await call(hass, "get_queue"))["queue_run"]
    assert (paused["phase"], paused["ends_at"]) == ("paused", None)
