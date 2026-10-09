"""Robots show their faults as soon as they appear; see dev doc "Gerätefehler"."""

from typing import cast

import pytest
from homeassistant.components.vacuum.const import VacuumEntityFeature
from homeassistant.components.websocket_api.connection import ActiveConnection
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.vacuum_orchestrator.api.websocket import (
    TYPE_SUBSCRIBE,
    websocket_subscribe,
)
from tests.test_configuration_api import call, configured  # noqa: F401
from tests.test_websocket import Connection


@pytest.mark.usefixtures("configured")
async def test_a_fault_is_shown_and_signalled_without_a_commit(
    hass: HomeAssistant,
) -> None:
    vacuum = er.async_get(hass).async_get_or_create("vacuum", "demo", "fault")
    attributes = {"supported_features": int(VacuumEntityFeature.CLEAN_AREA)}
    hass.states.async_set(vacuum.entity_id, "docked", attributes)
    await call(
        hass,
        "add_robot",
        configuration={"robot_entity_id": vacuum.entity_id, "fixed_mode": "vacuum"},
    )
    await hass.async_block_till_done()
    assert (await call(hass, "get_robots"))["robots"][0]["faults"] == []
    subscriber = Connection()
    websocket_subscribe(
        hass, cast(ActiveConnection, subscriber), {"id": 1, "type": TYPE_SUBSCRIBE}
    )
    await hass.async_block_till_done()
    before = len(subscriber.events)

    hass.states.async_set(vacuum.entity_id, "error", attributes)
    await hass.async_block_till_done()

    changed = set().union(
        *(event["changed"] for _id, event in subscriber.events[before:])
    )
    assert "robots" in changed
    assert (await call(hass, "get_robots"))["robots"][0]["faults"] == [
        {
            "code": "device_error",
            "source": "robot",
            "operations": ["mop", "vacuum", "vacuum_and_mop"],
            "entity_id": vacuum.entity_id,
        }
    ]
