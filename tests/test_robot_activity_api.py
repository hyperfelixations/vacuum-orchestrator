"""Robots show what they do and whether VOI runs it; see dev doc "Gerätezustand"."""

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
async def test_a_run_started_outside_voi_is_shown_as_external(
    hass: HomeAssistant,
) -> None:
    vacuum = er.async_get(hass).async_get_or_create("vacuum", "demo", "activity")
    attributes = {"supported_features": int(VacuumEntityFeature.CLEAN_AREA)}
    hass.states.async_set(vacuum.entity_id, "docked", attributes)
    await call(
        hass,
        "add_robot",
        configuration={"robot_entity_id": vacuum.entity_id, "fixed_mode": "vacuum"},
    )
    await hass.async_block_till_done()

    async def activity() -> dict[str, object]:
        return (await call(hass, "get_robots"))["robots"][0]["activity"]

    assert await activity() == {"phase": "docked", "external": False}
    subscriber = Connection()
    websocket_subscribe(
        hass, cast(ActiveConnection, subscriber), {"id": 1, "type": TYPE_SUBSCRIBE}
    )
    await hass.async_block_till_done()
    before = len(subscriber.events)

    hass.states.async_set(vacuum.entity_id, "cleaning", attributes)
    await hass.async_block_till_done()
    assert await activity() == {"phase": "cleaning", "external": True}
    changed = set().union(
        *(event["changed"] for _id, event in subscriber.events[before:])
    )
    assert "robots" in changed

    hass.states.async_set(vacuum.entity_id, "returning", attributes)
    await hass.async_block_till_done()
    assert await activity() == {"phase": "returning", "external": True}

    hass.states.async_set(vacuum.entity_id, "docked", attributes)
    await hass.async_block_till_done()
    assert await activity() == {"phase": "docked", "external": False}
    assert (await call(hass, "get_queue"))["jobs"] == []
