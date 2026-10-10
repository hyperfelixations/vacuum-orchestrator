"""Optional entities retain the room registry as their only data source."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vacuum_orchestrator.const import CONF_INSTALLATION_ID, DOMAIN
from custom_components.vacuum_orchestrator.domain.completion import (
    CleaningReceipt,
    CleaningSource,
    CompletionQuality,
)
from custom_components.vacuum_orchestrator.domain.releases import ReleaseKind
from custom_components.vacuum_orchestrator.domain.types import OperationKind
from custom_components.vacuum_orchestrator.sensor import RoomCleaningSensor
from custom_components.vacuum_orchestrator.switch import RoomReleaseSwitch
from tests.test_configuration_api import configured  # noqa: F401
from tests.test_runtime import MemoryBackend


@pytest.mark.usefixtures("configured")
async def test_room_entity_values_commands_and_clock_are_derived(hass, request):
    runtime = request.getfixturevalue("configured").runtime_data
    core = runtime.orchestrator
    room_id = await core.rooms.async_create("Study")
    switch = RoomReleaseSwitch(runtime, room_id, "release")
    last = RoomCleaningSensor(runtime, room_id, OperationKind.VACUUM, "last_cleaning")
    elapsed = RoomCleaningSensor(
        runtime, room_id, OperationKind.VACUUM, "elapsed_seconds"
    )
    due = RoomCleaningSensor(runtime, room_id, OperationKind.VACUUM, "due")
    assert last.native_value is None and last.extra_state_attributes["quality"] is None
    assert due.native_value == "disabled"
    assert not switch.is_on
    await switch.async_turn_on()
    assert switch.is_on
    await switch.async_turn_off()
    assert not switch.is_on
    now = datetime.now(UTC)
    await core.rooms.async_record_receipt(
        CleaningReceipt(
            "receipt",
            CleaningSource.VOI,
            "attempt",
            (room_id,),
            OperationKind.VACUUM,
            now - timedelta(hours=1),
            CompletionQuality.DERIVED,
            ("normal_end",),
        )
    )
    assert last.native_value == now - timedelta(hours=1)
    assert elapsed.native_value >= 3600
    assert elapsed.extra_state_attributes["quality"] == "derived"
    assert last.extra_state_attributes["last_confirmed"] is None
    await core.rooms.async_record_receipt(
        CleaningReceipt(
            "receipt",
            CleaningSource.VOI,
            "attempt",
            (room_id,),
            OperationKind.VACUUM,
            now - timedelta(hours=1),
            CompletionQuality.CONFIRMED,
            ("confirmed",),
        )
    )
    assert last.extra_state_attributes["last_confirmed"]
    assert last.available
    last.hass = hass
    last.async_write_ha_state = MagicMock()
    await last.async_added_to_hass()
    commit = core.state.commit_id
    last._tick(now)
    core.notify_runtime_change()
    assert last.async_write_ha_state.call_count == 2
    assert core.state.commit_id == commit
    last._call_on_remove_callbacks()
    await core.rooms.async_disable(room_id)
    assert not last.available


async def test_room_entities_register_disabled_and_follow_new_rooms(
    hass, enable_custom_integrations, monkeypatch
):
    MemoryBackend.data = None
    monkeypatch.setattr(
        "custom_components.vacuum_orchestrator.runtime.HomeAssistantSnapshotBackend",
        MemoryBackend,
    )
    assert await async_setup_component(hass, DOMAIN, {})
    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_INSTALLATION_ID: DOMAIN}, version=2
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    core = entry.runtime_data.orchestrator
    room_id = await core.rooms.async_create("Room")
    await hass.async_block_till_done()
    entries = [
        item
        for item in er.async_get(hass).entities.values()
        if item.config_entry_id == entry.entry_id and room_id in item.unique_id
    ]
    assert len(entries) == 7
    assert all(
        item.disabled_by is er.RegistryEntryDisabler.INTEGRATION for item in entries
    )
    await core.rooms.async_grant(
        room_id,
        ReleaseKind.PERMANENT,
    )
    await hass.async_block_till_done()
    assert (
        len(
            [
                item
                for item in er.async_get(hass).entities.values()
                if room_id in item.unique_id
            ]
        )
        == 7
    )
    await hass.config_entries.async_unload(entry.entry_id)


async def _loaded(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch):
    MemoryBackend.data = None
    monkeypatch.setattr(
        "custom_components.vacuum_orchestrator.runtime.HomeAssistantSnapshotBackend",
        MemoryBackend,
    )
    assert await async_setup_component(hass, DOMAIN, {})
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_INSTALLATION_ID: DOMAIN, "auto_discover_robots": False},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_every_entity_belongs_to_the_service_device_with_a_translated_name(
    hass, enable_custom_integrations, monkeypatch
):
    entry = await _loaded(hass, monkeypatch)
    room_id = await entry.runtime_data.orchestrator.rooms.async_create("Study")
    await hass.async_block_till_done()

    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, DOMAIN), entry.entry_id
    )
    assert device is not None
    assert (device.name, device.entry_type) == (
        "Vacuum Orchestrator",
        dr.DeviceEntryType.SERVICE,
    )
    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(registry, entry.entry_id)
    assert {item.device_id for item in entries} == {device.id}
    integration = Path(__file__).parents[1] / "custom_components" / DOMAIN
    english = json.loads(
        (integration / "translations/en.json").read_text(encoding="utf-8")
    )
    assert {(item.domain, item.translation_key) for item in entries} == {
        (domain, key) for domain, keys in english["entity"].items() for key in keys
    }
    icons = json.loads((integration / "icons.json").read_text(encoding="utf-8"))
    assert {domain: set(keys) for domain, keys in icons["entity"].items()} == {
        domain: set(keys) for domain, keys in english["entity"].items()
    }

    def friendly_name(domain: str, key: str) -> str:
        entity_id = registry.async_get_entity_id(domain, DOMAIN, f"{DOMAIN}_{key}")
        assert entity_id is not None
        return hass.states.get(entity_id).attributes["friendly_name"]

    assert friendly_name("sensor", "queue_mode") == "Vacuum Orchestrator Queue mode"
    assert friendly_name("binary_sensor", "needs_attention") == (
        "Vacuum Orchestrator Needs attention"
    )
    switch = registry.async_get_entity_id(
        "switch", DOMAIN, f"{DOMAIN}_room_{room_id}_release"
    )
    registry.async_update_entity(switch, disabled_by=None)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert friendly_name("switch", f"room_{room_id}_release") == (
        "Vacuum Orchestrator Study release"
    )
    # The name follows a renamed room without a reload.
    await entry.runtime_data.orchestrator.rooms.async_update(
        room_id, lambda room: replace(room, name="Office")
    )
    await hass.async_block_till_done()
    assert friendly_name("switch", f"room_{room_id}_release") == (
        "Vacuum Orchestrator Office release"
    )
