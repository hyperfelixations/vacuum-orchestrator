"""Recordings of the real integration match the committed ones.

See dev doc "Aufzeichnungen". `pytest tests/contract --update-recordings`
rewrites them; review their diff like code.
"""

import json

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from custom_components.vacuum_orchestrator.const import DOMAIN
from tests.contract.recorder import (
    RECORDINGS,
    Session,
    deterministic,
    provenance,
    voi_commit,
)
from tests.contract.scenarios import SCENARIOS, Stage, entry, home_of
from tests.test_runtime import MemoryBackend


@pytest.mark.parametrize("name", sorted(SCENARIOS))
async def test_recording_matches_the_integration(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    enable_custom_integrations: None,
    monkeypatch: pytest.MonkeyPatch,
    freezer: FrozenDateTimeFactory,
    request: pytest.FixtureRequest,
    name: str,
) -> None:
    deterministic(monkeypatch, freezer)
    MemoryBackend.data = None
    monkeypatch.setattr(
        "custom_components.vacuum_orchestrator.runtime.HomeAssistantSnapshotBackend",
        MemoryBackend,
    )
    scenario = SCENARIOS[name]
    home = scenario.household(hass)
    assert await async_setup_component(hass, DOMAIN, {})
    session = await Session.connect(hass, hass_ws_client, freezer)
    await scenario.run(Stage(hass, home, session, entry()))
    await session.settle()

    path = RECORDINGS / f"{name}.json"
    update = request.config.getoption("--update-recordings")
    committed = None if update else json.loads(path.read_text(encoding="utf-8"))
    recorded = {
        # The commit a recording was made on is no drift.
        "provenance": provenance(
            voi_commit() if committed is None else committed["provenance"]["voi_commit"]
        ),
        "scenario": name,
        "description": scenario.description,
        "home": home_of(home),
        "steps": session.steps,
    }
    if committed is None:
        path.write_text(
            json.dumps(recorded, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        return
    hint = "review, then run pytest tests/contract --update-recordings"
    for index, (old, new) in enumerate(
        zip(committed["steps"], recorded["steps"], strict=False)
    ):
        assert old == new, f"{name} drifts at step {index}; {hint}"
    assert committed == recorded, f"{name} drifts; {hint}"


def test_every_recording_belongs_to_a_scenario() -> None:
    assert {path.stem for path in RECORDINGS.glob("*.json")} == set(SCENARIOS)
