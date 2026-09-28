"""Consumer fixtures and shipped HA metadata must match real public responses."""

import json
from pathlib import Path

import yaml
from homeassistant.helpers.selector import validate_selector

from custom_components.vacuum_orchestrator.api import websocket as ws
from custom_components.vacuum_orchestrator.api.actions import async_setup_actions
from custom_components.vacuum_orchestrator.const import DOMAIN
from tests.test_websocket import Connection, StubOrchestrator, _install_runtime

ROOT = Path(__file__).parents[1]


def subset(expected, actual):
    if isinstance(expected, dict):
        for key, value in expected.items():
            assert key in actual
            subset(value, actual[key])
    else:
        assert expected == actual


async def test_v2_consumer_fixture_retains_existing_fields_and_values(hass):
    core = StubOrchestrator()
    _install_runtime(hass, core)
    connection = Connection()
    ws.websocket_job_get(hass, connection, {"id": 1, "job_id": "job"})
    await hass.async_block_till_done()
    expected = json.loads(
        (ROOT / "tests/fixtures/contracts/api_v2_job.json").read_text()
    )
    subset(expected, connection.results[0][1])


async def test_all_actions_have_descriptions_and_valid_selectors(hass):
    await async_setup_actions(hass)
    services = yaml.safe_load(
        (ROOT / "custom_components" / DOMAIN / "services.yaml").read_text()
    )
    assert set(services) == set(hass.services.async_services()[DOMAIN])
    for name, service in services.items():
        assert service["name"] and service["description"], name
        for field in service.get("fields", {}).values():
            validate_selector(field["selector"])


def test_distribution_metadata_and_translations_are_consistent():
    integration = ROOT / "custom_components" / DOMAIN
    manifest = json.loads((integration / "manifest.json").read_text())
    assert {
        "domain",
        "name",
        "version",
        "documentation",
        "issue_tracker",
        "codeowners",
    } <= manifest.keys()
    assert manifest["version"] == "0.1.0"
    assert json.loads((ROOT / "hacs.json").read_text())["homeassistant"] == "2026.9.0"
    assert (
        (integration / "brand/icon.png").read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    )
    strings = json.loads((integration / "strings.json").read_text(encoding="utf-8"))
    english = json.loads(
        (integration / "translations/en.json").read_text(encoding="utf-8")
    )
    german = json.loads(
        (integration / "translations/de.json").read_text(encoding="utf-8")
    )
    assert strings == english

    def keys(value):
        return (
            {key: keys(item) for key, item in value.items()}
            if isinstance(value, dict)
            else None
        )

    assert keys(strings) == keys(german)
