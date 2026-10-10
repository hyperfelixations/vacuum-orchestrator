"""A real Home Assistant process with VOI and simulated robots answers the owner.

Marked `e2e_host`: the default run skips it, CI's E2E job runs
`pytest -m e2e_host --no-cov tests/e2e_host` after installing
`requirements-e2e.txt`. See dev doc "E2E-Host".
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Iterator
from itertools import count
from typing import Any

import aiohttp
import pytest

from tests.e2e_host.host import LOG, Host, running

pytestmark = pytest.mark.e2e_host
OWN_LOGGERS = re.compile(r"(custom_components|vacuum_orchestrator|voi_e2e|voi_sim)")


@pytest.fixture(scope="module")
def host(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Host]:
    """One host for the module; its log must stay free of VOI errors."""
    config = tmp_path_factory.mktemp("e2e_host")
    with running(household="two_robots", install_voi=True, config=config) as started:
        yield started
    log = (started.config / LOG).read_text(encoding="utf-8", errors="replace")
    problems = [
        line
        for line in log.splitlines()
        if ("ERROR" in line or "Detected blocking call" in line)
        and OWN_LOGGERS.search(line)
    ]
    assert not problems, "\n".join(problems)


class Client:
    """The owner's WebSocket connection, as the frontend opens it."""

    def __init__(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        self.ws = ws
        self._ids = count(1)

    async def call(self, message: dict[str, Any]) -> Any:
        """Send a message and return its result; events are skipped."""
        sent = next(self._ids)
        await self.ws.send_json({"id": sent, **message})
        while True:
            frame = await self.ws.receive_json(timeout=30)
            if frame.get("id") == sent and frame["type"] in {"result", "pong"}:
                assert frame.get("success", True), frame
                return frame.get("result", frame)

    async def query(self, name: str, **parameters: Any) -> Any:
        """Run a VOI configuration query."""
        return await self.call(
            {
                "type": "vacuum_orchestrator/configuration/get",
                "query": name,
                "parameters": parameters,
            }
        )

    async def action(self, name: str, **data: Any) -> Any:
        """Call a VOI action and return its response."""
        result = await self.call(
            {
                "type": "call_service",
                "domain": "vacuum_orchestrator",
                "service": name,
                "service_data": data,
                "return_response": True,
            }
        )
        return result["response"]


def _with_client(host: Host, scenario: Any) -> Any:
    async def run() -> Any:
        async with (
            aiohttp.ClientSession() as http,
            http.ws_connect(f"{host.url}/api/websocket") as ws,
        ):
            assert (await ws.receive_json())["type"] == "auth_required"
            await ws.send_json(
                {"type": "auth", "access_token": host.session["access_token"]}
            )
            assert (await ws.receive_json())["type"] == "auth_ok"
            return await scenario(Client(ws), http)

    return asyncio.run(run())


def test_the_owner_reaches_the_real_frontend_and_websocket(host: Host) -> None:
    async def scenario(client: Client, http: aiohttp.ClientSession) -> None:
        assert (await client.call({"type": "ping"}))["type"] == "pong"
        # Confirmed, so HA never reverts to the default on all interfaces.
        server = await client.call({"type": "http/config"})
        assert (server["active_config_type"], server["revert_at"]) == ("stable", None)
        assert server["stable"]["server_host"] == ["127.0.0.1"]
        config = await client.call({"type": "get_config"})
        assert config["state"] == "RUNNING"
        assert config["recovery_mode"] is False
        assert {"frontend", "vacuum_orchestrator", "roborock", "voi_sim"} <= set(
            config["components"]
        )
        async with http.get(host.url) as page:
            assert page.status == 200
            assert "<html" in (await page.text()).lower()

    _with_client(host, scenario)


def test_voi_finds_both_simulated_robots_with_their_maps(host: Host) -> None:
    async def scenario(client: Client, _http: aiohttp.ClientSession) -> None:
        robots = {
            robot["name"]: robot
            for robot in (await client.query("get_robots"))["robots"]
        }
        assert sorted(robots) == ["Flitzi", "Saugi"]
        assert [item["name"] for item in robots["Saugi"]["maps"]] == [
            "Erdgeschoss",
            "Obergeschoss",
        ]

    _with_client(host, scenario)


def test_a_job_runs_on_the_simulated_roborock_and_completes(host: Host) -> None:
    async def scenario(client: Client, _http: aiohttp.ClientSession) -> None:
        areas = {
            area["name"]: area["area_id"]
            for area in await client.call({"type": "config/area_registry/list"})
        }
        kitchen = [areas["Küche"]]
        saugi = (await client.call({"type": "voi_e2e/robots"}))["Saugi"]
        await client.action("release_room", areas=kitchen, kind="permanent")
        job = (
            await client.action(
                "create_job",
                areas=kitchen,
                mode="vacuum",
                robot_id="vacuum.saugi",
                start=True,
            )
        )["job_id"]

        async def sent() -> Any:
            robots = await client.call({"type": "voi_e2e/robots"})
            commands = robots["Saugi"]["commands"][len(saugi["commands"]) :]
            return (
                any(item["command"] == "app_segment_clean" for item in commands)
                and commands
            )

        commands = await _until(sent, "Saugi's clean command")
        assert {
            "command": "app_segment_clean",
            "params": [{"segments": [16], "repeat": 1}],
        } in commands
        await client.call({"type": "voi_e2e/advance", "seconds": 3600})

        async def job_state() -> Any:
            detail = await client.call(
                {"type": "vacuum_orchestrator/job/get", "job_id": job}
            )
            return detail if detail["state"] == "completed" else None

        detail = await _until(job_state, "the job's completion", 90)
        assert detail["completion"]["notes"] == []

    _with_client(host, scenario)


async def _until(read: Any, what: str, seconds: float = 30) -> Any:
    """Poll until `read` returns a truthy value; VOI settles in real time."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if value := await read():
            return value
        await asyncio.sleep(0.5)
    raise AssertionError(f"timed out waiting for {what}")
