"""Recordings of the real integration match the committed ones.

See dev doc "Aufzeichnungen". `pytest tests/contract --update-recordings`
rewrites them; review their diff like code.
"""

import json

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.translation import (
    _async_get_translations_cache as translation_cache,
)
from homeassistant.helpers.translation import (
    _TranslationsCacheData as TranslationsCacheData,
)
from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from tests.contract.recorder import (
    RECORDINGS,
    Session,
    deterministic,
    provenance,
    serve_frontend_messages,
    voi_commit,
)
from tests.contract.scenarios import SCENARIOS, Stage, entry, home_of
from tests.test_runtime import MemoryBackend


@pytest.mark.parametrize("name", sorted(SCENARIOS))
async def test_recording_matches_the_integration(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
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
    if scenario.installed:
        request.getfixturevalue("enable_custom_integrations")
    else:
        # HA's test plugin shares loaded translations between tests; a home
        # without the integration has none of its texts.
        translation_cache(hass).cache_data = TranslationsCacheData({}, {})
    home = scenario.household(hass)
    await serve_frontend_messages(hass)
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
        "hass": session.initial_hass,
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
