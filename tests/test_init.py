"""Config-entry lifecycle guard tests."""

import subprocess
import sys
from pathlib import Path
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
        "custom_components.vacuum_orchestrator.async_setup_orchestrator",
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


def test_the_package_loads_what_setup_needs_in_the_import_executor() -> None:
    """HA imports the package off the event loop; setup must import nothing.

    Imports inside setup functions run in the loop, where module-level I/O such
    as telemetry reading `strings.json` blocks it.
    """
    code = (
        "import sys, custom_components.vacuum_orchestrator as voi;"
        "print(sorted(m for m in sys.modules if m.startswith(voi.__name__ + '.')))"
    )
    loaded = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    for module in (
        "api.actions",
        "api.websocket",
        "card_presence",
        "configuration",
        "infrastructure.telemetry",
        "runtime",
    ):
        assert f"custom_components.vacuum_orchestrator.{module}'" in loaded, module
