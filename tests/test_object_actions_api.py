"""Rooms, templates and robots name the actions VOI allows now."""

from datetime import timedelta

import pytest
from homeassistant.components.vacuum.const import VacuumEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.vacuum_orchestrator.runtime import async_get_runtime
from tests.test_configuration_api import call, configured  # noqa: F401
from tests.test_job_actions_api import offered


@pytest.mark.usefixtures("configured")
async def test_rooms_offer_release_and_exclusion_by_their_state(
    hass: HomeAssistant,
) -> None:
    room = (await call(hass, "create_room", name="Office"))["room_id"]

    assert offered((await call(hass, "get_room", room_id=room))["actions"]) == {
        "release": None,
        "revoke": "room_not_released",
        "edit": None,
        "disable": None,
        "enable": "room_enabled",
        "create_job": None,
    }
    await call(hass, "disable_room", room_id=room)
    (listed,) = (await call(hass, "get_rooms"))["rooms"]
    assert offered(listed["actions"]) == {
        "release": "room_unavailable",
        "revoke": "room_not_released",
        "edit": None,
        "disable": "room_disabled",
        "enable": None,
        "create_job": "room_unavailable",
    }


@pytest.mark.usefixtures("configured")
async def test_templates_offer_a_reset_only_for_suppressed_demand(
    hass: HomeAssistant,
) -> None:
    room = (await call(hass, "create_room", name="Office"))["room_id"]
    template = (
        await call(hass, "save_template", name="Daily", intent={"areas": [room]})
    )["template_id"]

    (listed,) = (await call(hass, "get_templates"))["templates"]
    assert offered(listed["actions"]) == {
        "create_job": None,
        "edit": None,
        "remove": None,
        "reset_demand": "no_suppressed_demand",
    }
    core = async_get_runtime(hass).orchestrator
    commit = core.state.commit_id
    await call(hass, "reset_template_demand", template_id=template)
    assert core.state.commit_id == commit


@pytest.mark.usefixtures("configured")
async def test_robots_offer_configuration_and_return_by_lease_and_capability(
    hass: HomeAssistant,
) -> None:
    vacuum = er.async_get(hass).async_get_or_create("vacuum", "demo", "actions")
    hass.states.async_set(
        vacuum.entity_id,
        "docked",
        {"supported_features": int(VacuumEntityFeature.CLEAN_AREA)},
    )
    await call(
        hass,
        "add_robot",
        configuration={"robot_entity_id": vacuum.entity_id, "fixed_mode": "vacuum"},
    )
    await hass.async_block_till_done()

    (robot,) = (await call(hass, "get_robots"))["robots"]
    assert offered(robot["actions"]) == {
        "configure": None,
        "rename": None,
        "remove": None,
        "return_to_dock": "return_to_dock_unsupported",
    }


@pytest.mark.usefixtures("configured")
async def test_room_views_use_the_orchestrator_clock(hass: HomeAssistant) -> None:
    room = (await call(hass, "create_room", name="Office"))["room_id"]
    await call(hass, "release_room", room_id=room, kind="timed", duration_seconds=60)
    core = async_get_runtime(hass).orchestrator
    later = core.now() + timedelta(minutes=2)
    core._clock = lambda: later

    detail = await call(hass, "get_room", room_id=room)
    assert detail["released"] is False
    (listed,) = (await call(hass, "get_rooms"))["rooms"]
    assert listed["released"] is False
