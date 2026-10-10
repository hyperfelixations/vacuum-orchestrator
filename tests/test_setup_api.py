"""The card reads the setup status from VOI and records its completion."""

from typing import cast

import pytest
from homeassistant.components.websocket_api.connection import ActiveConnection
from homeassistant.core import HomeAssistant

from custom_components.vacuum_orchestrator.api.websocket import (
    TYPE_SUBSCRIBE,
    websocket_subscribe,
)
from custom_components.vacuum_orchestrator.infrastructure.codec import (
    decode_orchestrator_state,
)
from custom_components.vacuum_orchestrator.infrastructure.integrity import (
    verify_snapshot,
)
from custom_components.vacuum_orchestrator.runtime import async_get_runtime
from tests.test_configuration_api import call, configured  # noqa: F401
from tests.test_job_holds_api import Card
from tests.test_runtime import MemoryBackend
from tests.test_websocket import Connection


@pytest.mark.usefixtures("configured")
async def test_the_setup_is_pending_until_the_card_completes_it(
    hass: HomeAssistant,
) -> None:
    room = (await call(hass, "create_room", name="Office"))["room_id"]
    setup = await call(hass, "get_setup")
    assert setup["assistant_pending"] is True
    assert setup["completed_at"] is None
    assert setup["steps"] == {
        "robots": {"robot_ids": []},
        "rooms": {"room_ids": [room], "unreachable_room_ids": [room]},
        "defaults": {"configured": False},
        "queue": {"grace_seconds": 900.0, "start_delay_seconds": 5.0},
    }
    assert {"api_version", "commit_id", "runtime_id"} <= setup.keys()

    subscriber = Connection()
    websocket_subscribe(
        hass, cast(ActiveConnection, subscriber), {"id": 1, "type": TYPE_SUBSCRIBE}
    )
    await hass.async_block_till_done()
    before = len(subscriber.events)
    result = await Card(hass).command("complete_setup")
    await hass.async_block_till_done()

    changed = set().union(
        *(event["changed"] for _id, event in subscriber.events[before:])
    )
    assert "setup" in changed
    setup = await call(hass, "get_setup")
    assert setup["assistant_pending"] is False
    assert setup["completed_at"] == result["completed_at"]
    stored = async_get_runtime(hass).orchestrator.state.setup_completed_at
    assert stored is not None and stored.isoformat() == result["completed_at"]
    assert MemoryBackend.data is not None
    persisted = decode_orchestrator_state(verify_snapshot(MemoryBackend.data))
    assert persisted.setup_completed_at == stored
