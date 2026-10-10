"""Record what a client exchanges with the real integration over HA's WebSocket.

See dev doc "Aufzeichnungen". Frames are kept as received; only the transport
message ID is renumbered, because the harness interleaves its own pings.
"""

from __future__ import annotations

import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from itertools import count
from pathlib import Path
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.auth.const import GROUP_ID_ADMIN
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    CLIENT_ID,
    MockUser,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.typing import (
    MockHAClientWebSocket,
    WebSocketGenerator,
)

from custom_components.vacuum_orchestrator.const import API_VERSION, DOMAIN

ROOT = Path(__file__).parents[2]
RECORDINGS = Path(__file__).parent / "recordings"
FORMAT = 1
# Six real fractional digits, so consumers parse every timestamp they will see.
FROZEN_NOW = "2026-03-14T09:26:53.589793+00:00"
USER_ID = "0123456789abcdef0123456789abcdef"


def deterministic(
    monkeypatch: pytest.MonkeyPatch, freezer: FrozenDateTimeFactory
) -> None:
    """Freeze time and draw every ID the frames can contain from counters."""
    freezer.move_to(datetime.fromisoformat(FROZEN_NOW))
    ulids = count(1)
    registry = count(1)
    voi = count(1)

    def ulid() -> str:
        return f"01J{next(ulids):023d}"

    # Contexts, config entries and robot subentries.
    monkeypatch.setattr("homeassistant.core.ulid_now", ulid)
    monkeypatch.setattr("homeassistant.util.ulid.ulid_now", ulid)
    # Entity and device registry IDs.
    monkeypatch.setattr(
        "homeassistant.util.uuid.getrandbits", lambda _bits: next(registry)
    )
    monkeypatch.setattr(
        "custom_components.vacuum_orchestrator.runtime.new_id",
        lambda: f"00000000-0000-4000-8000-{next(voi):012d}",
    )


async def admin_token(hass: HomeAssistant) -> str:
    """An access token of an administrator with a fixed user ID."""
    user = MockUser(id=USER_ID, name="Recording")
    user.groups = [await hass.auth.async_get_group(GROUP_ID_ADMIN)]
    user.add_to_hass(hass)
    refresh = await hass.auth.async_create_refresh_token(user, CLIENT_ID)
    return hass.auth.async_create_access_token(refresh)


class Session:
    """One card connection whose frames and surrounding steps are recorded."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: MockHAClientWebSocket,
        freezer: FrozenDateTimeFactory,
    ) -> None:
        self.hass = hass
        self.client = client
        self.freezer = freezer
        self.steps: list[dict[str, Any]] = []
        self._ids = count(1)
        # Transport ID sent → ID in the recording.
        self._recorded: dict[int, int] = {}
        self._results: dict[int, dict[str, Any]] = {}
        self._stalled = False

    @classmethod
    async def connect(
        cls,
        hass: HomeAssistant,
        hass_ws_client: WebSocketGenerator,
        freezer: FrozenDateTimeFactory,
    ) -> Session:
        """Open an authenticated connection as the card does."""
        client = await hass_ws_client(hass, await admin_token(hass))
        return cls(hass, client, freezer)

    async def send(self, message: dict[str, Any]) -> int:
        """Send a message without waiting for its answer; return its recorded ID."""
        sent = next(self._ids)
        recorded = len(self._recorded) + 1
        self._recorded[sent] = recorded
        self.steps.append({"send": {"id": recorded, **message}})
        await self.client.send_json({"id": sent, **message})
        return recorded

    async def request(self, message: dict[str, Any]) -> dict[str, Any]:
        """Send a message and return its result frame once everything settled."""
        recorded = await self.send(message)
        await self.settle(until=recorded)
        return self._results[recorded]

    async def query(self, name: str, /, **parameters: Any) -> Any:
        """Run a configuration query and return its result."""
        return self._success(
            await self.request(
                {
                    "type": f"{DOMAIN}/configuration/get",
                    "query": name,
                    "parameters": parameters,
                }
            )
        )

    async def command(self, name: str, /, **parameters: Any) -> dict[str, Any]:
        """Run a configuration command and return its result frame."""
        return await self.request(
            {
                "type": f"{DOMAIN}/configuration/command",
                "command": name,
                "parameters": parameters,
            }
        )

    async def action(self, name: str, /, **data: Any) -> dict[str, Any]:
        """Call an action as the card does and return its result frame."""
        return await self.request(action(name, **data))

    async def read(self) -> None:
        """Load the views the card shows: queue, jobs, rooms, robots, setup."""
        await self.request({"type": f"{DOMAIN}/queue/get"})
        jobs = self._success(await self.request({"type": f"{DOMAIN}/jobs/list"}))
        for job in jobs["jobs"]:
            await self.request({"type": f"{DOMAIN}/job/get", "job_id": job["job_id"]})
        for name in ("get_rooms", "get_robots", "get_setup"):
            await self.query(name)

    async def home(self, change: str) -> None:
        """Note a change in the home outside the card, then let VOI react."""
        self.steps.append({"home": change})
        await self.settle()

    async def advance(self, seconds: float) -> None:
        """Let time pass after VOI observed the current state."""
        await self.settle()
        self.steps.append({"advance": seconds})
        self.freezer.tick(timedelta(seconds=seconds))
        async_fire_time_changed(self.hass)
        await self.settle()

    @asynccontextmanager
    async def stalled(self) -> AsyncIterator[None]:
        """Settle without waiting for work that a stalled robot holds up."""
        self._stalled = True
        try:
            yield
        finally:
            self._stalled = False

    async def settle(self, *, until: int | None = None) -> None:
        """Receive every frame the server sent up to now (and the given result)."""
        if not self._stalled:
            await self.hass.async_block_till_done()
        while until is not None and until not in self._results:
            self._receive(await self.client.receive_json())
        ping = next(self._ids)
        await self.client.send_json({"id": ping, "type": "ping"})
        while (frame := await self.client.receive_json())["id"] != ping:
            self._receive(frame)
        assert frame["type"] == "pong"

    def _receive(self, frame: dict[str, Any]) -> None:
        frame["id"] = self._recorded[frame["id"]]
        self.steps.append({"receive": frame})
        if frame["type"] == "result":
            self._results[frame["id"]] = frame

    @staticmethod
    def _success(frame: dict[str, Any]) -> Any:
        assert frame["success"], frame
        return frame["result"]


def action(name: str, /, **data: Any) -> dict[str, Any]:
    """The message calling an action with its response, as the card sends it."""
    return {
        "type": "call_service",
        "domain": DOMAIN,
        "service": name,
        "service_data": data,
        "return_response": True,
    }


def voi_commit() -> str:
    """The VOI commit a recording was made on (its working tree may differ)."""
    return subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()


def provenance(commit: str) -> dict[str, Any]:
    """Where a recording comes from."""
    return {
        "format": FORMAT,
        "api_version": API_VERSION,
        "voi_commit": commit,
        "ha_version": HA_VERSION,
        "frozen_now": FROZEN_NOW,
    }
