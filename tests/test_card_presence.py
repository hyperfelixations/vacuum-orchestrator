"""A repair issue points to the companion card while it is not installed."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest
from homeassistant.components.lovelace.const import LOVELACE_DATA
from homeassistant.components.lovelace.resources import ResourceYAMLCollection
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vacuum_orchestrator.card_presence import (
    CARD_URL,
    ISSUE_ID,
    async_track_card,
)
from custom_components.vacuum_orchestrator.const import CONF_INSTALLATION_ID, DOMAIN
from custom_components.vacuum_orchestrator.infrastructure.integrity import JsonObject


def _issue(hass: HomeAssistant) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_ID)


async def test_storage_resources_are_tracked_including_hacs_query(
    hass: HomeAssistant,
) -> None:
    assert await async_setup_component(hass, "lovelace", {})
    resources = hass.data[LOVELACE_DATA].resources
    stop = await async_track_card(hass)

    issue = _issue(hass)
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.WARNING
    assert not issue.is_fixable
    assert issue.learn_more_url == CARD_URL
    assert issue.translation_placeholders == {"url": CARD_URL}

    item = await resources.async_create_item(
        {
            "res_type": "module",
            "url": "/hacsfiles/vacuum-orchestrator-card/"
            "vacuum-orchestrator-card.js?hacstag=12345",
        }
    )
    await hass.async_block_till_done()
    assert _issue(hass) is None

    await resources.async_delete_item(item["id"])
    await hass.async_block_till_done()
    assert _issue(hass) is not None

    stop()
    await resources.async_create_item(
        {"res_type": "module", "url": "/local/vacuum-orchestrator-card.js"}
    )
    await hass.async_block_till_done()
    assert _issue(hass) is not None


async def test_yaml_dev_build_counts_and_missing_lovelace_is_silent(
    hass: HomeAssistant,
) -> None:
    stop = await async_track_card(hass)
    stop()
    assert _issue(hass) is None

    hass.data[LOVELACE_DATA] = SimpleNamespace(
        resources=ResourceYAMLCollection(
            [{"type": "module", "url": "/local/vacuum-orchestrator-card-dev.js"}]
        )
    )
    await async_track_card(hass)
    assert _issue(hass) is None


async def test_unreadable_resources_do_not_raise_an_issue(
    hass: HomeAssistant,
) -> None:
    class Broken:
        async def async_get_info(self) -> dict[str, int]:
            raise OSError("unreadable")

    hass.data[LOVELACE_DATA] = SimpleNamespace(resources=Broken())
    await async_track_card(hass)
    assert _issue(hass) is None


class MemoryBackend:
    atomic_writes = True

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.data: JsonObject | None = None

    async def async_load_raw(self) -> JsonObject | None:
        return deepcopy(self.data)

    async def async_save_raw(self, data: JsonObject) -> None:
        self.data = deepcopy(data)


async def test_ignored_issue_survives_reload_and_entry_removal_clears_it(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "custom_components.vacuum_orchestrator.runtime.HomeAssistantSnapshotBackend",
        MemoryBackend,
    )
    assert await async_setup_component(hass, "lovelace", {})
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_INSTALLATION_ID: DOMAIN, "auto_discover_robots": False},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert _issue(hass) is not None

    ir.async_get(hass).async_ignore(DOMAIN, ISSUE_ID, True)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    issue = _issue(hass)
    assert issue is not None and issue.dismissed_version is not None

    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert _issue(hass) is None
