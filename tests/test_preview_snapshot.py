"""A preview answers from one moment: observed robots, state and metadata."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.vacuum_orchestrator.api.websocket import (
    websocket_configuration_get,
)
from custom_components.vacuum_orchestrator.domain.releases import ReleaseKind
from custom_components.vacuum_orchestrator.domain.types import VacuumLevel
from custom_components.vacuum_orchestrator.runtime import async_get_runtime
from tests.application.test_dispatch_races import gate_observation
from tests.application.test_orchestrator import RecordingAdapter, RecordingBackend
from tests.test_configuration_api import call, configured  # noqa: F401
from tests.test_websocket import Connection


def _robot(robot_id: str, room: str) -> RecordingAdapter:
    adapter = RecordingAdapter(RecordingBackend(), robot_id, targets=(room,))
    adapter._profile = replace(
        adapter.profile,
        capabilities=replace(
            adapter.profile.capabilities,
            vacuum_levels=frozenset({VacuumLevel.LOW, VacuumLevel.MAXIMUM}),
        ),
    )
    return adapter


async def _preview_while(
    hass: HomeAssistant,
    adapter: RecordingAdapter,
    monkeypatch: pytest.MonkeyPatch,
    room: str,
    change: Any,
) -> dict[str, Any]:
    """Run a draft preview and apply `change` while robots are observed."""
    entered, release = gate_observation(adapter, monkeypatch)
    connection = Connection()
    websocket_configuration_get(
        hass,
        connection,  # type: ignore[arg-type]
        {"id": 1, "query": "preview_job", "parameters": {"areas": [room]}},
    )
    await asyncio.wait_for(entered.wait(), 1)
    try:
        await change()
    finally:
        release.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert not connection.errors
    return connection.results[0][1]


@pytest.mark.usefixtures("configured")
async def test_defaults_changed_during_observation_answer_with_their_commit(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = async_get_runtime(hass)
    await runtime.controller.scheduler.async_close()
    core = runtime.orchestrator
    room = (await call(hass, "create_room", name="Office"))["room_id"]
    await core.rooms.async_grant(room, ReleaseKind.PERMANENT)
    adapter = _robot("robot", room)
    await core.async_replace_adapters({"robot": adapter})

    async def change() -> None:
        await call(hass, "configure_job_defaults", passes=2, vacuum_power="low")

    preview = await _preview_while(hass, adapter, monkeypatch, room, change)

    assert preview["commit_id"] == core.state.commit_id
    assert preview["passes"] == 2
    assert preview["settings"]["vacuum_power"]["requested"] == "low"
    assert preview["robots"][0]["settings"] == [
        {"name": "vacuum_power", "requested": "low", "applied": "low"}
    ]


@pytest.mark.usefixtures("configured")
async def test_robots_replaced_during_observation_are_observed_again(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = async_get_runtime(hass)
    await runtime.controller.scheduler.async_close()
    core = runtime.orchestrator
    room = (await call(hass, "create_room", name="Office"))["room_id"]
    await core.rooms.async_grant(room, ReleaseKind.PERMANENT)
    adapter = _robot("robot", room)
    await core.async_replace_adapters({"robot": adapter})

    async def change() -> None:
        await core.async_replace_adapters({"other": _robot("other", room)})

    preview = await _preview_while(hass, adapter, monkeypatch, room, change)

    assert [item["robot_id"] for item in preview["robots"]] == ["other"]
    assert preview["robots"][0]["startable_now"]
    assert preview["startable_now"] and preview["reason"] is None


@pytest.mark.usefixtures("configured")
async def test_job_explanations_observe_replaced_robots_again(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = async_get_runtime(hass)
    await runtime.controller.scheduler.async_close()
    core = runtime.orchestrator
    room = (await call(hass, "create_room", name="Office"))["room_id"]
    await core.rooms.async_grant(room, ReleaseKind.PERMANENT)
    adapter = _robot("robot", room)
    await core.async_replace_adapters({"robot": adapter})
    job = (await call(hass, "create_job", areas=[room]))["job_id"]
    entered, release = gate_observation(adapter, monkeypatch)
    connection = Connection()
    websocket_configuration_get(
        hass,
        connection,  # type: ignore[arg-type]
        {"id": 1, "query": "get_job_execution", "parameters": {"job_id": job}},
    )
    await asyncio.wait_for(entered.wait(), 1)
    try:
        await core.async_replace_adapters({"other": _robot("other", room)})
    finally:
        release.set()
    await hass.async_block_till_done(wait_background_tasks=True)

    (robot,) = connection.results[0][1]["robots"]
    assert (robot["robot_id"], robot["eligible"]) == ("other", True)
