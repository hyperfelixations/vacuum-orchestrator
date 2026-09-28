"""Optional entities retain the room registry as their only data source."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
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
    assert last.name == "Study vacuum last cleaning"
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
    await core.rooms.async_remove(room_id)
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
