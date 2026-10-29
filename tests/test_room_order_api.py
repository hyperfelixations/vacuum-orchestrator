"""Clients list rooms in the order VOI sets, most due first by default (ROOM-13)."""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant

from custom_components.vacuum_orchestrator.domain.rooms import RoomOrder
from custom_components.vacuum_orchestrator.infrastructure.codec import (
    decode_orchestrator_state,
    encode_orchestrator_state,
)
from custom_components.vacuum_orchestrator.runtime import async_get_runtime
from tests.test_configuration_api import call, configured  # noqa: F401


async def _names(hass: HomeAssistant) -> list[str]:
    return [room["name"] for room in (await call(hass, "get_rooms"))["rooms"]]


@pytest.mark.usefixtures("configured")
async def test_rooms_come_most_due_first_until_the_order_is_by_name(
    hass: HomeAssistant,
) -> None:
    for name in ("Study", "Attic", "Kitchen"):
        await call(hass, "create_room", name=name)
    rooms = {
        room["name"]: room["room_id"]
        for room in (await call(hass, "get_rooms"))["rooms"]
    }
    await call(
        hass,
        "update_room",
        room_id=rooms["Kitchen"],
        configuration={"due_policy": {"basis": "calendar", "vacuum_seconds": 3600}},
    )

    listed = await call(hass, "get_rooms")
    assert listed["room_order"] == "due"
    # Kitchen was never cleaned and has an interval; the others have none.
    assert await _names(hass) == ["Kitchen", "Attic", "Study"]

    response = await call(hass, "configure_room_order", room_order="name")
    assert response["room_order"] == "name"
    assert await _names(hass) == ["Attic", "Kitchen", "Study"]
    assert (await call(hass, "get_rooms"))["room_order"] == "name"
    # The order belongs to the rooms' read model, not to the queue's.
    assert "room_order" not in await call(hass, "get_queue")
    checked = await call(
        hass, "validate", command="configure_room_order", data={"room_order": "size"}
    )
    assert checked["valid"] is False
    assert [item["path"] for item in checked["errors"]] == [["room_order"]]


@pytest.mark.usefixtures("configured")
async def test_the_order_is_stored_and_an_older_snapshot_lists_by_due(
    hass: HomeAssistant,
) -> None:
    await call(hass, "configure_room_order", room_order="name")
    state = async_get_runtime(hass).orchestrator.state
    encoded = encode_orchestrator_state(state)
    assert encoded["room_order"] == "name"
    assert decode_orchestrator_state(encoded).room_order is RoomOrder.NAME
    del encoded["room_order"]
    assert decode_orchestrator_state(encoded).room_order is RoomOrder.DUE
