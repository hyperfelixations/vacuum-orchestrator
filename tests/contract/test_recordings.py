"""Every scenario records what a card exchanges with the real integration.

See dev doc "Aufzeichnungen". Nothing is committed: a consumer such as the
card records them on demand with `python -m tests.contract --record-to <dir>`.
"""

import json
import re
import subprocess
from pathlib import Path
from typing import Any

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

from custom_components.vacuum_orchestrator.const import API_VERSION, DOMAIN
from tests.contract.__main__ import arguments
from tests.contract.recorder import (
    FORMAT,
    ROOT,
    Session,
    deterministic,
    dump,
    provenance,
    serve_frontend_messages,
    voi_commit,
    voi_dirty,
)
from tests.contract.scenarios import (
    CONSERVATORY_DOOR,
    SCENARIOS,
    Stage,
    entry,
    home_of,
)
from tests.test_runtime import MemoryBackend

# Recorded twice per run: same code, same bytes.
REPEATED = ("mop_run", "showcase")
# A recording in this run, by scenario, for the repeat.
RECORDED: dict[str, str] = {}
# What a recording must never contain: machine paths, mail and network addresses.
PRIVATE = re.compile(
    r"[A-Za-z]:\\\\|/(?:home|Users|root|tmp)/|[\w.+-]+@[\w-]+\.[\w.]+"
    r"|\b(?!127\.0\.0\.1\b)(?:\d{1,3}\.){3}\d{1,3}\b"
)


@pytest.fixture
async def record(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    monkeypatch: pytest.MonkeyPatch,
    freezer: FrozenDateTimeFactory,
    request: pytest.FixtureRequest,
) -> Any:
    """Record one scenario in this test's Home Assistant."""

    async def run(name: str) -> dict[str, Any]:
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
        return {
            "provenance": provenance(voi_commit(), dirty=voi_dirty()),
            "scenario": name,
            "description": scenario.description,
            "home": home_of(home),
            "hass": session.initial_hass,
            "steps": session.steps,
        }

    return run


def check(name: str, recording: dict[str, Any]) -> None:
    """A recording a consumer can rely on."""
    assert recording["provenance"]["format"] == FORMAT
    assert recording["provenance"]["api_version"] == API_VERSION
    sent = {
        step["send"]["id"]: step["send"]
        for step in recording["steps"]
        if "send" in step
    }
    results = [
        step["receive"]
        for step in recording["steps"]
        if step.get("receive", {}).get("type") == "result"
    ]
    assert sorted(result["id"] for result in results) == sorted(sent), (
        f"{name}: every message has exactly one result"
    )
    refused = [result for result in results if not result["success"]]
    assert len(refused) == SCENARIOS[name].refusals, f"{name}: {refused}"
    for result in refused:
        error, message = result["error"], sent[result["id"]]
        assert error["code"], error
        assert error["message"], error
        if DOMAIN in (message["type"].split("/")[0], message.get("domain")):
            assert error["translation_domain"] == DOMAIN, error
            assert error["translation_key"], error
    leaked = PRIVATE.search(dump(recording))
    assert leaked is None, f"{name} contains {leaked.group()!r}"


@pytest.mark.parametrize("name", sorted(SCENARIOS))
async def test_scenario_records(
    record: Any, request: pytest.FixtureRequest, name: str
) -> None:
    recording = await record(name)
    check(name, recording)
    text = RECORDED.setdefault(name, dump(recording))
    target = request.config.getoption("--record-to")
    if target is not None:
        target.mkdir(parents=True, exist_ok=True)
        (target / f"{name}.json").write_text(text, encoding="utf-8", newline="\n")


def answers(recording: dict[str, Any], query: str) -> list[Any]:
    """The results of every configuration query of one kind."""
    asked = {
        step["send"]["id"]
        for step in recording["steps"]
        if step.get("send", {}).get("query") == query
    }
    return [
        step["receive"]["result"]
        for step in recording["steps"]
        if step.get("receive", {}).get("id") in asked
    ]


async def test_the_showcase_records_what_the_editor_and_the_map_show(
    record: Any,
) -> None:
    recording = await record("showcase")
    maps = {
        entity_id: state["a"]["entity_picture"]
        for entity_id, state in recording["hass"]["states"].items()
        if entity_id.startswith("image.")
    }
    assert sorted(maps) == ["image.rocky_lower_floor", "image.rocky_main_floor"]
    assert all(picture.startswith("/api/image_proxy/") for picture in maps.values())
    assert len(answers(recording, "preview_job")) == 4
    conservatory = next(
        room
        for result in answers(recording, "get_rooms")[-1:]
        for room in result["rooms"]
        if room["name"] == "Conservatory"
    )
    assert [item["entity_id"] for item in conservatory["requirements"]] == [
        CONSERVATORY_DOOR
    ]


async def test_a_card_checks_the_integration_again_after_a_reload(
    record: Any,
) -> None:
    steps = (await record("reload"))["steps"]
    reloaded = steps.index({"home": "Home Assistant reloads the integration"})
    assert {"type": f"{DOMAIN}/queue/get", "offset": 0, "limit": 1} in [
        {key: value for key, value in step["send"].items() if key != "id"}
        for step in steps[reloaded:]
        if "send" in step
    ]


@pytest.mark.parametrize("name", REPEATED)
async def test_a_scenario_records_the_same_bytes_again(record: Any, name: str) -> None:
    first = RECORDED.get(name)
    if first is None:
        pytest.skip(f"runs after test_scenario_records[{name}] in the same run")
    again = json.loads(dump(await record(name)))
    for index, (old, new) in enumerate(
        zip(json.loads(first)["steps"], again["steps"], strict=False)
    ):
        assert old == new, f"{name} differs at step {index}"
    assert dump(again) == first, name


def test_recordings_are_not_committed() -> None:
    tracked = subprocess.run(
        ["git", "ls-files", "tests/contract"],
        cwd=ROOT,
        capture_output=True,
        check=True,
        text=True,
    ).stdout.split()
    assert not [path for path in tracked if path.endswith(".json")]


def test_the_entry_point_records_every_scenario_into_its_directory(
    tmp_path: Path,
) -> None:
    command = arguments(tmp_path / "out")
    assert command[command.index("--record-to") + 1] == str(tmp_path / "out")
    assert command[command.index("-k") + 1] == "test_scenario_records"
    assert "--no-cov" in command
