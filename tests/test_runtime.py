"""Tests for single-runtime composition and physical ownership."""

from copy import deepcopy
from dataclasses import replace
from types import MappingProxyType
from typing import cast

import pytest
from homeassistant.components.vacuum.const import VacuumEntityFeature
from homeassistant.components.websocket_api.connection import ActiveConnection
from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vacuum_orchestrator.api.websocket import (
    TYPE_SUBSCRIBE,
    websocket_subscribe,
)
from custom_components.vacuum_orchestrator.const import (
    ADAPTER_ROBOROCK,
    API_VERSION,
    CONF_ADAPTER,
    CONF_INSTALLATION_ID,
    CONF_ROBOT_ENTITY_ID,
    CONF_ROBOT_REGISTRY_ID,
    CONF_TARGET_AREAS,
    DOMAIN,
    SUBENTRY_TYPE_ROBOT,
)
from custom_components.vacuum_orchestrator.domain.due import DueBasis, DuePolicy
from custom_components.vacuum_orchestrator.domain.errors import ConflictError
from custom_components.vacuum_orchestrator.domain.intents import JobIntent, TargetRef
from custom_components.vacuum_orchestrator.domain.releases import ReleaseKind
from custom_components.vacuum_orchestrator.domain.requirements import StateRequirement
from custom_components.vacuum_orchestrator.domain.types import CleaningMode, JobState
from custom_components.vacuum_orchestrator.infrastructure.integrity import JsonObject
from custom_components.vacuum_orchestrator.runtime import (
    async_get_registry,
    async_get_runtime,
    async_setup_orchestrator,
    async_unload_orchestrator,
)
from tests.test_websocket import Connection


class MemoryBackend:
    atomic_writes = True
    data: JsonObject | None = None

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    async def async_load_raw(self) -> JsonObject | None:
        return deepcopy(self.data)

    async def async_save_raw(self, data: JsonObject) -> None:
        type(self).data = deepcopy(data)


def _entry(installation_id: str = DOMAIN) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={CONF_INSTALLATION_ID: installation_id},
        subentries_data=[
            {
                "subentry_type": SUBENTRY_TYPE_ROBOT,
                "title": "Roborock",
                "unique_id": "registry",
                "data": {
                    CONF_ROBOT_ENTITY_ID: "vacuum.roborock",
                    CONF_ROBOT_REGISTRY_ID: "registry",
                    CONF_ADAPTER: ADAPTER_ROBOROCK,
                    CONF_TARGET_AREAS: ["kitchen"],
                },
            }
        ],
    )


async def test_runtime_composes_one_global_owner_and_releases_it(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    MemoryBackend.data = None
    monkeypatch.setattr(
        "custom_components.vacuum_orchestrator.runtime.HomeAssistantSnapshotBackend",
        MemoryBackend,
    )
    hass.states.async_set(
        "vacuum.roborock",
        "docked",
        {"supported_features": int(VacuumEntityFeature.CLEAN_AREA)},
    )
    first = _entry()
    second = _entry("other")

    assert await async_setup_orchestrator(hass, first)
    assert async_get_runtime(hass) is async_get_registry(hass)[first.entry_id]
    assert async_get_runtime(hass).orchestrator.state.installation_id == DOMAIN
    with pytest.raises(ConflictError, match="source_robot_already_owned"):
        await async_setup_orchestrator(hass, second)

    assert await async_unload_orchestrator(hass, first)
    MemoryBackend.data = None
    assert await async_setup_orchestrator(hass, second)
    assert await async_unload_orchestrator(hass, second)
    assert await async_unload_orchestrator(hass, second)


def test_runtime_resolution_rejects_zero_or_multiple_entries(
    hass: HomeAssistant,
) -> None:
    with pytest.raises(ConflictError, match="not_loaded"):
        async_get_runtime(hass)
    registry = async_get_registry(hass)
    registry["one"] = object()  # type: ignore[assignment]
    registry["two"] = object()  # type: ignore[assignment]
    with pytest.raises(ConflictError, match="multiple"):
        async_get_runtime(hass)


async def test_runtime_discovers_robot_and_preserves_explicit_removal(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    MemoryBackend.data = None
    monkeypatch.setattr(
        "custom_components.vacuum_orchestrator.runtime.HomeAssistantSnapshotBackend",
        MemoryBackend,
    )
    registry = er.async_get(hass)
    vacuum = registry.async_get_or_create("vacuum", "demo", "unit")
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_INSTALLATION_ID: DOMAIN})
    entry.add_to_hass(hass)
    await async_setup_orchestrator(hass, entry)
    await hass.async_block_till_done()
    assert len(entry.subentries) == 1
    subentry = next(iter(entry.subentries.values()))
    assert subentry.data[CONF_ROBOT_REGISTRY_ID] == vacuum.id
    runtime = entry.runtime_data
    hass.config_entries.async_remove_subentry(entry, subentry.subentry_id)
    await hass.async_block_till_done()
    assert not entry.subentries
    assert vacuum.id in entry.data["excluded_robot_registry_ids"]
    assert entry.runtime_data is runtime
    registry.async_update_entity(vacuum.entity_id, new_entity_id="vacuum.renamed")
    await hass.async_block_till_done()
    assert not entry.subentries
    await async_unload_orchestrator(hass, entry)


async def test_setup_never_moves_foreign_entities_or_devices_into_areas(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    MemoryBackend.data = None
    monkeypatch.setattr(
        "custom_components.vacuum_orchestrator.runtime.HomeAssistantSnapshotBackend",
        MemoryBackend,
    )
    areas = ar.async_get(hass)
    office = areas.async_create("Office")
    kitchen = areas.async_create("Kitchen")
    vendor = MockConfigEntry(domain="roborock")
    vendor.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=vendor.entry_id, identifiers={("roborock", "unit")}
    )
    dr.async_get(hass).async_update_device(device.id, area_id=office.id)
    registry = er.async_get(hass)
    vacuum = registry.async_get_or_create(
        "vacuum", "roborock", "unit", config_entry=vendor, device_id=device.id
    )
    registry.async_update_entity_options(
        vacuum.entity_id, "vacuum", {"area_mapping": {kitchen.id: ["0_16"]}}
    )
    foreign_before = {
        entry.entity_id: entry.area_id for entry in registry.entities.values()
    }

    assert await async_setup_component(hass, DOMAIN, {})
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_INSTALLATION_ID: DOMAIN})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.subentries
    assert {
        entity_id: registry.async_get(entity_id).area_id  # type: ignore[union-attr]
        for entity_id in foreign_before
    } == foreign_before
    assert dr.async_get(hass).async_get(device.id).area_id == office.id  # type: ignore[union-attr]
    own = [
        item
        for item in registry.entities.values()
        if item.config_entry_id == entry.entry_id
    ]
    assert own
    assert all(item.device_id is None and item.area_id is None for item in own)


async def test_room_state_change_wakes_queue_and_unload_fences_active_job(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    MemoryBackend.data = None
    monkeypatch.setattr(
        "custom_components.vacuum_orchestrator.runtime.HomeAssistantSnapshotBackend",
        MemoryBackend,
    )
    registry = er.async_get(hass)
    vacuum = registry.async_get_or_create(
        "vacuum", "demo", "unit", suggested_object_id="test"
    )
    area = ar.async_get(hass).async_create("Kitchen")
    registry.async_update_entity_options(
        vacuum.entity_id, "vacuum", {"area_mapping": {area.id: ["16"]}}
    )
    attrs = {
        "supported_features": int(
            VacuumEntityFeature.CLEAN_AREA | VacuumEntityFeature.STOP
        )
    }
    hass.states.async_set(vacuum.entity_id, "docked", attrs)
    hass.states.async_set("binary_sensor.door", "off")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_INSTALLATION_ID: DOMAIN, "auto_discover_robots": False},
    )
    entry.add_to_hass(hass)
    subentry = ConfigSubentry(
        data=MappingProxyType(
            {
                CONF_ROBOT_REGISTRY_ID: vacuum.id,
                CONF_ROBOT_ENTITY_ID: vacuum.entity_id,
                CONF_ADAPTER: "home_assistant",
                CONF_TARGET_AREAS: [area.id],
                "fixed_mode": "vacuum",
            }
        ),
        subentry_type="robot",
        title="Robot",
        unique_id=vacuum.id,
    )
    hass.config_entries.async_add_subentry(entry, subentry)
    calls = []

    async def clean(call: ServiceCall):
        calls.append(call)
        hass.states.async_set(vacuum.entity_id, "cleaning", attrs)

    hass.services.async_register("vacuum", "clean_area", clean)
    await async_setup_orchestrator(hass, entry)
    await hass.async_block_till_done()
    orchestrator = entry.runtime_data.orchestrator
    room = orchestrator.rooms.registry.resolve(area.id)
    await orchestrator.rooms.async_grant(room.room_id, ReleaseKind.ONCE)
    await orchestrator.rooms.async_update(
        room.room_id,
        lambda current: replace(
            current, requirements=(StateRequirement("binary_sensor.door"),)
        ),
    )
    job = await orchestrator.async_create_job(
        JobIntent((TargetRef(area.id),), CleaningMode.VACUUM)
    )
    await orchestrator.async_run_queue()
    await hass.async_block_till_done()
    assert not calls
    hass.states.async_set("binary_sensor.door", "on")
    await hass.async_block_till_done()
    assert len(calls) == 1
    assert orchestrator.state.jobs[job].state is JobState.RUNNING
    assert orchestrator.rooms.registry.resolve(room.room_id).release.consumed
    await async_unload_orchestrator(hass, entry)
    assert orchestrator.state.jobs[job].state is JobState.NEEDS_ATTENTION
    hass.states.async_set("binary_sensor.door", "off")
    await hass.async_block_till_done()
    assert len(calls) == 1


async def test_occupancy_binding_follows_rename_and_never_reuses_deleted_entity(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    MemoryBackend.data = None
    monkeypatch.setattr(
        "custom_components.vacuum_orchestrator.runtime.HomeAssistantSnapshotBackend",
        MemoryBackend,
    )
    registry = er.async_get(hass)
    sensor = registry.async_get_or_create("binary_sensor", "demo", "occupied")
    hass.states.async_set(sensor.entity_id, "on")
    area = ar.async_get(hass).async_create("Room")
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_INSTALLATION_ID: DOMAIN})
    entry.add_to_hass(hass)
    await async_setup_orchestrator(hass, entry)
    await hass.async_block_till_done()
    core = entry.runtime_data.orchestrator
    room_id = core.rooms.registry.resolve(area.id).room_id
    await core.rooms.async_update(
        room_id,
        lambda room: replace(
            room,
            due_policy=DuePolicy(
                DueBasis.OCCUPIED,
                3600,
                occupancy_entity_id=sensor.entity_id,
                occupancy_entity_registry_id=sensor.id,
            ),
        ),
    )
    await hass.async_block_till_done()
    assert core.rooms.registry.resolve(room_id).occupancy.occupied is True
    epoch = core.rooms.registry.resolve(room_id).occupancy.epoch
    registry.async_update_entity(
        sensor.entity_id, new_entity_id="binary_sensor.renamed"
    )
    hass.states.async_set("binary_sensor.renamed", "off")
    await hass.async_block_till_done()
    assert core.rooms.registry.resolve(room_id).occupancy.occupied is False
    assert core.rooms.registry.resolve(room_id).occupancy.epoch == epoch
    registry.async_remove("binary_sensor.renamed")
    await hass.async_block_till_done()
    assert core.rooms.registry.resolve(room_id).occupancy.occupied is None
    assert hass.states.get(sensor.entity_id).state == "on"
    await async_unload_orchestrator(hass, entry)


async def test_home_assistant_stop_closes_runtime_before_later_unload(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
):
    MemoryBackend.data = None
    monkeypatch.setattr(
        "custom_components.vacuum_orchestrator.runtime.HomeAssistantSnapshotBackend",
        MemoryBackend,
    )
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_INSTALLATION_ID: DOMAIN})
    entry.add_to_hass(hass)
    await async_setup_orchestrator(hass, entry)
    await hass.async_block_till_done()
    core = entry.runtime_data.orchestrator
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    with pytest.raises(ConflictError, match="shutting_down"):
        await core.async_create_job(
            JobIntent((TargetRef("room"),), CleaningMode.VACUUM)
        )
    await async_unload_orchestrator(hass, entry)


async def test_view_subscription_survives_runtime_reload(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    MemoryBackend.data = None
    monkeypatch.setattr(
        "custom_components.vacuum_orchestrator.runtime.HomeAssistantSnapshotBackend",
        MemoryBackend,
    )
    hass.states.async_set(
        "vacuum.roborock",
        "docked",
        {"supported_features": int(VacuumEntityFeature.CLEAN_AREA)},
    )
    entry = _entry()
    entry.add_to_hass(hass)
    assert await async_setup_orchestrator(hass, entry)
    await hass.async_block_till_done()
    first_runtime = async_get_runtime(hass).orchestrator.runtime_id
    connection = Connection()
    websocket_subscribe(
        hass, cast(ActiveConnection, connection), {"id": 1, "type": TYPE_SUBSCRIBE}
    )

    assert await async_unload_orchestrator(hass, entry)
    assert connection.events[-1] == (1, {"api_version": API_VERSION, "loaded": False})
    assert await async_setup_orchestrator(hass, entry)
    await hass.async_block_till_done()

    event = connection.events[-1][1]
    assert event["loaded"] is True
    assert event["runtime_id"] == async_get_runtime(hass).orchestrator.runtime_id
    assert event["runtime_id"] != first_runtime
    assert await async_unload_orchestrator(hass, entry)
