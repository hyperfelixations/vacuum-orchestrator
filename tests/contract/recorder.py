"""Record what a card exchanges with the real integration over HA's WebSocket.

See dev doc "Aufzeichnungen". Frames are kept as received; only the transport
message ID is renumbered, because the harness interleaves its own pings. A
second connection reads what the HA frontend gives a card as `hass`.
"""

from __future__ import annotations

import subprocess
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from itertools import count
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.auth.const import GROUP_ID_ADMIN
from homeassistant.components import websocket_api
from homeassistant.components.frontend import websocket_get_translations
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
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
FORMAT = 2
# What the card reads, as it reads it; see the card's scope loaders.
CARD_PAGE_SIZE = 25
CARD_COLLECTION_LIMIT = 100
OPEN_JOB_STATES = ["dispatching", "running", "canceling", "needs_attention"]
CARD_COLLECTIONS = ("get_rooms", "get_robots", "get_robot_candidates", "get_templates")
CARD_LANGUAGES = ("en", "de")
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
    # The diagnostics' pseudonym key; only VOI's telemetry, HA draws tokens too.
    monkeypatch.setattr(
        "custom_components.vacuum_orchestrator.infrastructure.telemetry.secrets",
        SimpleNamespace(token_bytes=bytes),
    )


async def serve_frontend_messages(hass: HomeAssistant) -> None:
    """Answer what HA's frontend and the card ask besides the integration.

    The current user comes from `auth`, the registry lists from `config`; the
    error texts from the frontend's own handler, without its web assets.
    """
    for component in ("auth", "config"):
        assert await async_setup_component(hass, component, {})
    websocket_api.async_register_command(hass, websocket_get_translations)


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
        frontend: MockHAClientWebSocket,
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
        self.frontend = frontend
        self._frontend_ids = count(1)
        # What `hass` showed when the recording began, and its latest parts.
        self.initial_hass: dict[str, Any] = {}
        self._hass: dict[str, Any] = {}
        self._registry_read: Any = None
        self._pending_events: list[dict[str, Any]] = []

    @classmethod
    async def connect(
        cls,
        hass: HomeAssistant,
        hass_ws_client: WebSocketGenerator,
        freezer: FrozenDateTimeFactory,
    ) -> Session:
        """Open an authenticated connection as the card does, and the frontend's."""
        token = await admin_token(hass)
        session = cls(
            hass,
            await hass_ws_client(hass, token),
            await hass_ws_client(hass, token),
            freezer,
        )
        await session._start_frontend()
        return session

    async def _start_frontend(self) -> None:
        """Subscribe to entity states as the frontend does and keep the start."""
        await self._frontend_request({"type": "subscribe_entities"})
        await self._frontend_request({"type": "ping"})
        (start,) = self._frontend_events()
        self._hass = {**await self._hass_parts(), "states": self._states(start["a"])}
        self.initial_hass = dict(self._hass)

    async def _frontend_request(self, message: dict[str, Any]) -> Any:
        sent = next(self._frontend_ids)
        await self.frontend.send_json({"id": sent, **message})
        while (frame := await self.frontend.receive_json())["type"] == "event" or (
            frame["id"] != sent
        ):
            self._pending_events.append(frame["event"])
        if frame["type"] == "pong":
            return None
        assert frame["success"], frame
        return frame["result"]

    def _frontend_events(self) -> list[dict[str, Any]]:
        events, self._pending_events = self._pending_events, []
        return events

    def _states(self, entity_ids: Iterable[str]) -> dict[str, Any]:
        """The current states as the frontend receives them, by entity ID.

        Without contexts: HA draws their IDs in varying order and cards do not
        read them.
        """
        return {
            entity_id: {
                key: value
                for key, value in state.as_compressed_state.items()
                if key != "c"
            }
            for entity_id in sorted(entity_ids)
            if (state := self.hass.states.get(entity_id)) is not None
        }

    async def _hass_parts(self) -> dict[str, Any]:
        """The parts of `hass` a card reads, from the frontend's own messages."""
        config = await self._frontend_request({"type": "get_config"})
        return {
            # Only these: the rest names the machine's paths.
            "config": {
                "components": sorted(config["components"]),
                "version": config["version"],
            },
            "user": await self._frontend_request({"type": "auth/current_user"}),
            "areas": await self._frontend_request(
                {"type": "config/area_registry/list"}
            ),
            "entities": await self._frontend_request(
                {"type": "config/entity_registry/list_for_display"}
            ),
            "services": {
                DOMAIN: sorted(self.hass.services.async_services_for_domain(DOMAIN))
            },
        }

    async def _record_hass(self) -> None:
        """Note what changed in `hass` since the last step."""
        await self._frontend_request({"type": "ping"})
        changes: dict[str, Any] = {}
        # One step for everything that changed meanwhile: HA reports states of
        # one moment in varying order.
        changed = {
            entity_id
            for event in self._frontend_events()
            for kind in ("a", "c", "r")
            for entity_id in event.get(kind, ())
        }
        if changed:
            states = self._states(changed)
            removed = sorted(changed - states.keys())
            changes["states"] = {"a": states} | ({"r": removed} if removed else {})
        for key, value in (await self._hass_parts()).items():
            if self._hass.get(key) != value:
                self._hass[key] = changes[key] = value
        if changes:
            self.steps.append({"hass": changes})

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
        return await self._change(
            {
                "type": f"{DOMAIN}/configuration/command",
                "command": name,
                "parameters": parameters,
            }
        )

    async def action(self, name: str, /, **data: Any) -> dict[str, Any]:
        """Call an action as the card does and return its result frame."""
        return await self._change(action(name, **data))

    async def _change(self, message: dict[str, Any]) -> dict[str, Any]:
        """Send a change; after a success the card reads its views again."""
        frame = await self.request(message)
        if frame["success"]:
            await self.read()
        return frame

    async def probe(self) -> None:
        """Check the integration as a card does before it reads."""
        await self.request({"type": f"{DOMAIN}/queue/get", "offset": 0, "limit": 1})

    async def read_static(self) -> None:
        """Read what a card loads once: the integration's error texts."""
        for language in CARD_LANGUAGES:
            await self.request(
                {
                    "type": "frontend/get_translations",
                    "language": language,
                    "category": "exceptions",
                    "integration": [DOMAIN],
                }
            )

    async def read(self) -> None:
        """Read what a card shows on its queue view, as the card asks for it."""
        await self.request(
            {"type": f"{DOMAIN}/queue/get", "offset": 0, "limit": CARD_PAGE_SIZE}
        )
        await self.request(
            {
                "type": f"{DOMAIN}/jobs/list",
                "offset": 0,
                "limit": CARD_COLLECTION_LIMIT,
                "states": OPEN_JOB_STATES,
            }
        )
        for name in CARD_COLLECTIONS:
            await self.query(name, offset=0, limit=CARD_COLLECTION_LIMIT)
        await self.query("get_setup")
        # The card reads the registry again only once it changed.
        if self._hass["entities"] != self._registry_read:
            self._registry_read = self._hass["entities"]
            await self.request({"type": "config/entity_registry/list"})

    async def collection(self, name: str) -> Any:
        """Read a configuration collection as the card does: its first page."""
        return await self.query(name, offset=0, limit=CARD_COLLECTION_LIMIT)

    async def read_history(self) -> None:
        """Read the history view's first pages: jobs and cleaning runs."""
        await self.request(
            {"type": f"{DOMAIN}/jobs/list", "offset": 0, "limit": CARD_PAGE_SIZE}
        )
        await self.query("get_history", offset=0, limit=CARD_PAGE_SIZE)

    async def read_diagnostics(self) -> None:
        """Read the diagnostics view: the summary and the latest trace records."""
        await self.query("get_diagnostics")
        await self.query("get_trace", offset=0, limit=CARD_COLLECTION_LIMIT)

    async def read_manifest(self) -> dict[str, Any]:
        """Ask HA for the integration's manifest, as a card does while VOI is not
        loaded, and return the result frame."""
        return await self.request({"type": "manifest/get", "integration": DOMAIN})

    async def read_job(self, job_id: str) -> None:
        """Read what a card shows on a job's detail page."""
        await self.request({"type": f"{DOMAIN}/job/get", "job_id": job_id})
        await self.query("get_job_execution", job_id=job_id)
        await self.query(
            "get_trace", offset=0, limit=CARD_COLLECTION_LIMIT, job_id=job_id
        )

    async def home(self, change: str) -> None:
        """Note a change in the home outside the card, let VOI react and read."""
        self.steps.append({"home": change})
        await self.settle()
        await self.read()

    async def mark(self, label: str) -> None:
        """Name this point, so a consumer can open a card here.

        A card opened here checks the integration and reads, if it is loaded.
        """
        self.steps.append({"mark": label})
        if DOMAIN in self._hass["config"]["components"]:
            await self.probe()
            await self.read()

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
        await self._record_hass()

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
