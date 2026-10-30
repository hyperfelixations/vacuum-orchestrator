"""Every user Home Assistant lets control the robots may command VOI (ACT-07)."""

from __future__ import annotations

from typing import Any, cast

import pytest
from homeassistant.auth.models import User
from homeassistant.auth.permissions import PolicyPermissions
from homeassistant.components.websocket_api.connection import ActiveConnection
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import Unauthorized

from custom_components.vacuum_orchestrator.api.authorization import may_command
from custom_components.vacuum_orchestrator.api.websocket import (
    websocket_configuration_command,
    websocket_configuration_get,
)
from custom_components.vacuum_orchestrator.const import DOMAIN
from tests.test_configuration_api import add_robot, call, configured  # noqa: F401
from tests.test_robot_names import _vacuum
from tests.test_websocket import Connection
from tests.users import household_member


async def _create_room(hass: HomeAssistant, user: User) -> None:
    await hass.services.async_call(
        DOMAIN,
        "create_room",
        {"name": "Study"},
        blocking=True,
        context=Context(user_id=user.id),
    )


@pytest.mark.usefixtures("configured")
async def test_a_regular_user_may_command_like_an_administrator(
    hass: HomeAssistant, hass_admin_user: User
) -> None:
    user = await household_member(hass)
    assert not user.is_admin
    await _create_room(hass, user)
    await _create_room(hass, hass_admin_user)
    created = await hass.services.async_call(
        DOMAIN,
        "create_job",
        {"all_rooms": True, "mode": "vacuum"},
        blocking=True,
        return_response=True,
        context=Context(user_id=user.id),
    )
    assert created is not None and created["job_id"]
    assert (await call(hass, "get_rooms"))["total"] == 2


@pytest.mark.usefixtures("configured")
async def test_a_read_only_user_is_refused_every_command(
    hass: HomeAssistant, hass_read_only_user: User
) -> None:
    with pytest.raises(Unauthorized):
        await _create_room(hass, hass_read_only_user)
    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN,
            "create_job",
            {"all_rooms": True, "mode": "vacuum"},
            blocking=True,
            return_response=True,
            context=Context(user_id=hass_read_only_user.id),
        )
    assert (await call(hass, "get_rooms"))["total"] == 0


@pytest.mark.usefixtures("configured")
async def test_a_user_without_control_of_a_configured_robot_is_refused(
    hass: HomeAssistant,
) -> None:
    vacuum = _vacuum(hass)
    await add_robot(hass, vacuum.entity_id)
    await hass.async_block_till_done()
    user = await household_member(hass)
    assert may_command(hass, user)
    other = cast(Any, await household_member(hass))
    other.permissions = PolicyPermissions(
        {"entities": {"entity_ids": {"vacuum.elsewhere": True}}}, None
    )
    assert not may_command(hass, other)
    other.permissions = PolicyPermissions(
        {"entities": {"entity_ids": {vacuum.entity_id: True}}}, None
    )
    assert may_command(hass, other)


@pytest.mark.usefixtures("configured")
async def test_the_setup_tells_the_client_whether_its_user_may_command(
    hass: HomeAssistant, hass_read_only_user: User
) -> None:
    user = await household_member(hass)
    for who, allowed in ((user, True), (hass_read_only_user, False)):
        setup = await hass.services.async_call(
            DOMAIN,
            "get_setup",
            {},
            blocking=True,
            return_response=True,
            context=Context(user_id=who.id),
        )
        assert setup is not None and setup["permissions"] == {"command": allowed}
    automation = await call(hass, "get_setup")
    assert automation["permissions"] == {"command": True}

    connection = Connection()
    for message_id, who, allowed in (
        (1, user, True),
        (2, hass_read_only_user, False),
    ):
        connection.user = who
        websocket_configuration_get(
            hass,
            cast(ActiveConnection, connection),
            {"id": message_id, "query": "get_setup", "parameters": {}},
        )
        await hass.async_block_till_done()
        assert connection.results[-1][1]["permissions"] == {"command": allowed}


@pytest.mark.usefixtures("configured")
async def test_the_card_command_channel_follows_the_same_permission(
    hass: HomeAssistant, hass_read_only_user: User
) -> None:
    connection = Connection()
    connection.user = await household_member(hass)
    websocket_configuration_command(
        hass,
        cast(ActiveConnection, connection),
        {"id": 1, "command": "create_room", "parameters": {"name": "Study"}},
    )
    await hass.async_block_till_done()
    assert connection.results[0][1]["room_id"]
    connection.user = hass_read_only_user
    websocket_configuration_command(
        hass,
        cast(ActiveConnection, connection),
        {"id": 2, "command": "create_room", "parameters": {"name": "Hall"}},
    )
    await hass.async_block_till_done()
    assert connection.errors == [(2, "unauthorized", "Unauthorized")]
    assert (await call(hass, "get_rooms"))["total"] == 1
