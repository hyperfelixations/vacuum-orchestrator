"""The card holds jobs over WebSocket; actions accept its token, never a hold."""

from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.vacuum_orchestrator.api.websocket import (
    websocket_configuration_command,
)
from custom_components.vacuum_orchestrator.const import DOMAIN
from custom_components.vacuum_orchestrator.runtime import async_get_runtime
from tests.errors import raises_code
from tests.test_configuration_api import call, configured  # noqa: F401
from tests.test_websocket import Connection


class Card:
    """One card session talking to the real configuration command handler."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.connection = Connection()
        self.connection.user = SimpleNamespace(is_admin=True)  # type: ignore[attr-defined]
        self.message_id = 0

    async def command(self, command: str, **parameters: Any) -> dict[str, Any]:
        """Send one command; return its result or its error code."""
        self.message_id += 1
        websocket_configuration_command(
            self.hass,
            self.connection,
            {"id": self.message_id, "command": command, "parameters": parameters},
        )
        await self.hass.async_block_till_done()
        for message_id, result in self.connection.results:
            if message_id == self.message_id:
                return result
        (error,) = [
            item for item in self.connection.errors if item[0] == self.message_id
        ]
        return {"error": error[1]}


async def _job(hass: HomeAssistant) -> str:
    room = (await call(hass, "create_room", name="Office"))["room_id"]
    return (await call(hass, "create_job", areas=[room]))["job_id"]


@pytest.mark.usefixtures("configured")
async def test_a_card_holds_a_job_and_saves_it_through_the_action(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    job = await _job(hass)
    editor, other = Card(hass), Card(hass)

    held = await editor.command("hold_job", job_id=job, purpose="edit")
    expires_at = datetime.fromisoformat(held["expires_at"])
    assert held["job"]["job_id"] == job
    assert held["job"]["hold"] == {"purpose": "edit", "expires_at": held["expires_at"]}
    assert await other.command("hold_job", job_id=job, purpose="confirm") == {
        "error": "job_held"
    }
    detail = await call(hass, "get_job", job_id=job)
    assert detail["hold"] == held["job"]["hold"]
    assert held["hold_id"] not in str(detail)
    assert held["hold_id"] not in str(await call(hass, "get_queue"))

    freezer.tick(timedelta(seconds=30))
    renewed = await editor.command("renew_job_hold", hold_id=held["hold_id"])
    assert datetime.fromisoformat(renewed["expires_at"]) == expires_at + timedelta(
        seconds=30
    )
    with raises_code("job_held"):
        await call(hass, "update_job", job_id=job, note="Other card")
    with raises_code("job_held"):
        await call(hass, "delete_job", job_id=job)

    await call(hass, "update_job", job_id=job, note="Checked", hold_id=held["hold_id"])
    saved = await call(hass, "get_job", job_id=job)
    assert (saved["note"], saved["hold"]) == ("Checked", None)
    assert datetime.fromisoformat(saved["start_after"]) == datetime.fromisoformat(
        held["expires_at"]
    ) - timedelta(seconds=90 - 30 - 5)
    assert await editor.command("renew_job_hold", hold_id=held["hold_id"]) == {
        "error": "hold_expired"
    }

    confirm = await other.command("hold_job", job_id=job, purpose="confirm")
    await call(hass, "delete_job", job_id=job, hold_id=confirm["hold_id"])
    assert job not in async_get_runtime(hass).orchestrator.state.jobs
    for name in ("hold_job", "renew_job_hold", "release_job_hold"):
        assert not hass.services.has_service(DOMAIN, name)


@pytest.mark.usefixtures("configured")
async def test_a_lapsed_hold_wakes_the_scheduler_and_restarts_the_delay(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    core = async_get_runtime(hass).orchestrator
    job = await _job(hass)
    card = Card(hass)
    held = await card.command("hold_job", job_id=job, purpose="edit")
    await card.command("release_job_hold", hold_id=held["hold_id"])
    assert core.state.job_holds == {}
    repeated = await card.command("release_job_hold", hold_id=held["hold_id"])
    assert "error" not in repeated

    await card.command("hold_job", job_id=job, purpose="edit")
    freezer.tick(timedelta(seconds=90))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert core.state.job_holds == {}
    lapsed = datetime.fromisoformat(
        (await call(hass, "get_job", job_id=job))["created_at"]
    )
    assert core.state.jobs[job].start_after == lapsed + timedelta(seconds=95)


@pytest.mark.usefixtures("configured")
async def test_a_lapsed_delete_confirmation_cannot_delete_a_newer_version(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    job = await _job(hass)
    deleting, editing = Card(hass), Card(hass)
    stale = await deleting.command("hold_job", job_id=job, purpose="confirm")
    freezer.tick(timedelta(seconds=90))
    edit = await editing.command("hold_job", job_id=job, purpose="edit")
    await call(hass, "update_job", job_id=job, note="Newer", hold_id=edit["hold_id"])

    with raises_code("hold_expired"):
        await call(hass, "delete_job", job_id=job, hold_id=stale["hold_id"])
    assert (await call(hass, "get_job", job_id=job))["note"] == "Newer"
