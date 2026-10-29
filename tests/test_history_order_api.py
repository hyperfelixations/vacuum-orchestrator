"""History lists finished jobs by when they ended, the latest first (JOB-28)."""

from typing import cast

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.websocket_api.connection import ActiveConnection
from homeassistant.core import HomeAssistant

from custom_components.vacuum_orchestrator.api import websocket as websocket_api
from custom_components.vacuum_orchestrator.domain.types import JobState
from tests.test_configuration_api import call, configured  # noqa: F401
from tests.test_websocket import Connection


@pytest.mark.usefixtures("configured")
async def test_a_job_created_first_but_finished_last_leads_the_history(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    room = (await call(hass, "create_room", name="Office"))["room_id"]
    first = (await call(hass, "create_job", areas=[room]))["job_id"]
    freezer.tick(60)
    second = (await call(hass, "create_job", areas=[room]))["job_id"]
    freezer.tick(60)
    await call(hass, "cancel_job", job_id=second)
    freezer.tick(60)
    await call(hass, "cancel_job", job_id=first)

    connection = Connection()
    websocket_api.websocket_jobs_list(
        hass,
        cast(ActiveConnection, connection),
        {
            "id": 1,
            "type": websocket_api.TYPE_JOBS_LIST,
            "offset": 0,
            "limit": 10,
            "states": [JobState.CANCELLED],
        },
    )
    await hass.async_block_till_done()
    jobs = connection.results[0][1]["jobs"]
    assert [job["job_id"] for job in jobs] == [first, second]
    assert jobs[0]["finished_at"] > jobs[1]["finished_at"]
