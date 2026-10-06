"""Vacuum Orchestrator integration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.helpers import config_validation as cv

from .const import DOMAIN

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up integration-level API handlers."""
    from .api.actions import async_setup_actions
    from .api.websocket import async_setup_websocket
    from .runtime import async_get_registry

    async_get_registry(hass)
    await async_setup_actions(hass)
    async_setup_websocket(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the integration-wide orchestrator."""
    from .card_presence import async_track_card
    from .const import PLATFORMS
    from .runtime import async_setup_orchestrator

    if not await async_setup_orchestrator(hass, entry):
        return False
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(await async_track_card(hass))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload the integration-wide orchestrator."""
    from .const import PLATFORMS
    from .runtime import async_unload_orchestrator

    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    return await async_unload_orchestrator(hass, entry)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Drop the card hint, which outlives unloads to keep an ignore choice."""
    from .card_presence import async_remove_issue

    async_remove_issue(hass)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate older entries to the single-installation contract and ladders."""
    from .configuration import migrate_setting_mappings
    from .const import (
        CONF_INSTALLATION_ID,
        CONFIG_ENTRY_MINOR_VERSION,
        CONFIG_ENTRY_VERSION,
        DOMAIN,
    )

    if entry.version == 1:
        hass.config_entries.async_update_entry(
            entry,
            data={CONF_INSTALLATION_ID: DOMAIN},
            title="Vacuum Orchestrator",
            version=CONFIG_ENTRY_VERSION,
            minor_version=0,
        )
    if entry.version != CONFIG_ENTRY_VERSION:
        return False
    if entry.minor_version < 1:
        for subentry in list(entry.subentries.values()):
            data = migrate_setting_mappings(subentry.data)
            if data != subentry.data:
                hass.config_entries.async_update_subentry(entry, subentry, data=data)
        hass.config_entries.async_update_entry(
            entry, minor_version=CONFIG_ENTRY_MINOR_VERSION
        )
    return True
