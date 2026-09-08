"""Config-entry lifecycle guard tests."""

from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vacuum_orchestrator import async_setup_entry, async_unload_entry
from custom_components.vacuum_orchestrator.const import CONF_INSTALLATION_ID, DOMAIN


async def test_entry_setup_propagates_composition_failure(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_INSTALLATION_ID: DOMAIN})
    compose = AsyncMock(return_value=False)
    monkeypatch.setattr(
        "custom_components.vacuum_orchestrator.runtime.async_setup_orchestrator",
        compose,
    )

    assert not await async_setup_entry(hass, entry)
    compose.assert_awaited_once_with(hass, entry)


async def test_entry_unload_stops_when_platform_unload_fails(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_INSTALLATION_ID: DOMAIN})
    unload_platforms = AsyncMock(return_value=False)
    monkeypatch.setattr(hass.config_entries, "async_unload_platforms", unload_platforms)

    assert not await async_unload_entry(hass, entry)
