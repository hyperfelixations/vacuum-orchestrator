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


async def test_v4_consumer_fixture_retains_existing_fields_and_values(hass):
    core = StubOrchestrator()
    _install_runtime(hass, core)
    connection = Connection()
    ws.websocket_job_get(hass, connection, {"id": 1, "job_id": "job"})
    await hass.async_block_till_done()
    expected = json.loads(
        (ROOT / "tests/fixtures/contracts/api_v4_job.json").read_text()
    )
    subset(expected, connection.results[0][1])


async def test_all_actions_are_translated_with_icons_and_valid_selectors(hass):
    await async_setup_actions(hass)
    integration = ROOT / "custom_components" / DOMAIN
    services = yaml.safe_load((integration / "services.yaml").read_text())
    assert set(services) == set(hass.services.async_services()[DOMAIN])
    icons = json.loads((integration / "icons.json").read_text(encoding="utf-8"))
    assert set(icons["services"]) == set(services)
    for language in ("strings.json", "translations/de.json"):
        strings = json.loads((integration / language).read_text(encoding="utf-8"))
        assert set(strings["services"]) == set(services), language
        for name, entry in services.items():
            # Texts live only in the translations; see dev doc "Actions".
            service = entry or {}
            assert service.keys() <= {"fields"}, name
            text = strings["services"][name]
            assert text["name"] and text["description"], name
            fields = service.get("fields", {})
            assert set(text.get("fields", {})) == set(fields), name
            for key, field in fields.items():
                assert field.keys() <= {"required", "default", "advanced", "selector"}
                label = text["fields"][key]
                assert label["name"] and label["description"], (name, key)
                validate_selector(field["selector"])
                select = field["selector"].get("select", {})
                if "translation_key" in select:
                    options = strings["selector"][select["translation_key"]]
                    assert set(options["options"]) == set(select["options"])
                    assert all(options["options"].values())


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
    assert manifest["version"] == "0.1.1"
    # The tested baseline is the pinned HA test stack; HACS and README name it.
    (baseline,) = (
        line.removeprefix("homeassistant==")
        for line in (ROOT / "requirements-test.txt").read_text().splitlines()
        if line.startswith("homeassistant==")
    )
    assert baseline == "2026.10.0"
    assert json.loads((ROOT / "hacs.json").read_text())["homeassistant"] == baseline
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert f"**Home Assistant {baseline}** or newer" in readme
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
