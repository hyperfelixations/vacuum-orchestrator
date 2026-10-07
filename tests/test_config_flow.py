"""Tests for single-entry setup and Roborock subentries."""

from copy import deepcopy
from types import MappingProxyType, SimpleNamespace

import pytest
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    SOURCE_USER,
    ConfigSubentry,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vacuum_orchestrator import async_migrate_entry
from custom_components.vacuum_orchestrator.const import (
    CONF_INSTALLATION_ID,
    CONF_ROBOT_ENTITY_ID,
    CONF_ROBOT_REGISTRY_ID,
    CONF_TARGET_AREAS,
    DOMAIN,
    SUBENTRY_TYPE_ROBOT,
)
from custom_components.vacuum_orchestrator.infrastructure.integrity import JsonObject


class MemoryBackend:
    atomic_writes = True

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.data: JsonObject | None = None

    async def async_load_raw(self) -> JsonObject | None:
        return deepcopy(self.data)

    async def async_save_raw(self, data: JsonObject) -> None:
        self.data = deepcopy(data)


async def test_user_flow_creates_single_global_orchestrator(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "custom_components.vacuum_orchestrator.runtime.HomeAssistantSnapshotBackend",
        MemoryBackend,
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] == "create_entry"
    assert result["title"] == "Vacuum Orchestrator"
    assert result["data"] == {CONF_INSTALLATION_ID: DOMAIN}

    duplicate = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert duplicate["type"] == "abort"
    assert duplicate["reason"] == "single_instance_allowed"
    assert duplicate["translation_domain"] == "homeassistant"
    assert hass.config_entries.async_entries(DOMAIN)[0].unique_id is None


async def test_robot_subentry_persists_stable_registry_identity(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_INSTALLATION_ID: DOMAIN})
    entry.add_to_hass(hass)
    entity = er.async_get(hass).async_get_or_create(
        "vacuum", "roborock", "serial", suggested_object_id="robot"
    )
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_ROBOT), context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_ROBOT_ENTITY_ID: entity.entity_id, CONF_TARGET_AREAS: ["kitchen"]},
    )

    assert result["type"] == "create_entry"
    assert result["data"][CONF_ROBOT_REGISTRY_ID] == entity.id


async def test_robot_subentry_rejects_wrong_or_missing_entity(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_INSTALLATION_ID: DOMAIN})
    entry.add_to_hass(hass)
    wrong = er.async_get(hass).async_get_or_create(
        "vacuum",
        "demo",
        "serial",
        suggested_object_id="wrong",
        disabled_by=er.RegistryEntryDisabler.USER,
    )
    for entity_id, expected in (
        (wrong.entity_id, "vacuum_unavailable"),
        ("vacuum.missing", "entity_not_registered"),
    ):
        result = await hass.config_entries.subentries.async_init(
            (entry.entry_id, SUBENTRY_TYPE_ROBOT), context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"],
            {CONF_ROBOT_ENTITY_ID: entity_id, CONF_TARGET_AREAS: ["kitchen"]},
        )
        assert result["errors"] == {CONF_ROBOT_ENTITY_ID: expected}


async def test_pre_release_config_entry_migration_is_single_installation(
    hass: HomeAssistant,
) -> None:
    legacy = MockConfigEntry(
        domain=DOMAIN,
        title="Legacy Fleet",
        data={"fleet_id": "legacy"},
        version=1,
        minor_version=1,
    )
    legacy.add_to_hass(hass)

    assert await async_migrate_entry(hass, legacy)
    assert legacy.version == 2
    assert legacy.minor_version == 1
    assert legacy.title == "Vacuum Orchestrator"
    assert legacy.data == {CONF_INSTALLATION_ID: DOMAIN}

    unsupported = MockConfigEntry(domain=DOMAIN, data={}, version=99)
    assert not await async_migrate_entry(hass, unsupported)


async def test_minor_one_migration_moves_option_mappings_onto_the_ladders(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_INSTALLATION_ID: DOMAIN}, version=2, minor_version=0
    )
    entry.add_to_hass(hass)
    old = {
        CONF_ROBOT_ENTITY_ID: "vacuum.test",
        "vacuum_levels": {"low": "quiet", "medium": "balanced", "auto": "smart"},
        "water_levels": {
            "standard": "moderate",
            "maximum": "intense",
            "high": "high",
            "auto": "smart",
        },
        "mop_routes": {"deep": "deep", "auto": "smart"},
    }
    for unique_id, data in (("old", old), ("plain", {CONF_ROBOT_ENTITY_ID: "x"})):
        hass.config_entries.async_add_subentry(
            entry,
            ConfigSubentry(
                data=MappingProxyType(data),
                subentry_type=SUBENTRY_TYPE_ROBOT,
                title=unique_id,
                unique_id=unique_id,
            ),
        )

    assert await async_migrate_entry(hass, entry)

    migrated = {item.unique_id: item.data for item in entry.subentries.values()}
    assert migrated["old"]["vacuum_levels"] == {"low": "quiet", "standard": "balanced"}
    assert migrated["old"]["water_levels"] == {"high": "high", "medium": "moderate"}
    assert migrated["old"]["mop_routes"] == {"deep": "deep"}
    assert migrated["plain"] == {CONF_ROBOT_ENTITY_ID: "x"}
    assert entry.minor_version == 1
    assert await async_migrate_entry(hass, entry)
    assert {item.unique_id: item.data for item in entry.subentries.values()} == migrated


async def test_reconfigure_tracks_renames_and_blocks_active_robot(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_INSTALLATION_ID: DOMAIN})
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    entity = registry.async_get_or_create("vacuum", "demo", "robot")
    flow = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_ROBOT), context={"source": SOURCE_USER}
    )
    created = await hass.config_entries.subentries.async_configure(
        flow["flow_id"], {CONF_ROBOT_ENTITY_ID: entity.entity_id}
    )
    assert created["type"] == "create_entry"
    subentry = next(iter(entry.subentries.values()))
    registry.async_update_entity(entity.entity_id, new_entity_id="vacuum.renamed")
    flow = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_ROBOT),
        context={"source": SOURCE_RECONFIGURE, "subentry_id": subentry.subentry_id},
    )
    assert flow["step_id"] == "reconfigure"
    entry.runtime_data = SimpleNamespace(
        orchestrator=SimpleNamespace(
            state=SimpleNamespace(
                robot_leases={"source": SimpleNamespace(robot_id=subentry.subentry_id)}
            )
        )
    )
    result = await hass.config_entries.subentries.async_configure(
        flow["flow_id"],
        {CONF_ROBOT_ENTITY_ID: "vacuum.renamed", CONF_TARGET_AREAS: ["kitchen"]},
    )
    assert result["errors"] == {CONF_ROBOT_ENTITY_ID: "robot_busy"}
    entry.runtime_data.orchestrator.state.robot_leases.clear()
    result = await hass.config_entries.subentries.async_configure(
        flow["flow_id"],
        {CONF_ROBOT_ENTITY_ID: "vacuum.renamed", CONF_TARGET_AREAS: ["kitchen"]},
    )
    assert result["type"] == "abort"
    assert result["reason"] == "reconfigure_successful"
    assert (
        entry.subentries[subentry.subentry_id].data[CONF_ROBOT_REGISTRY_ID] == entity.id
    )
    assert (
        entry.subentries[subentry.subentry_id].data[CONF_ROBOT_ENTITY_ID]
        == "vacuum.renamed"
    )


async def test_reconfigure_cannot_replace_physical_entity(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_INSTALLATION_ID: DOMAIN})
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    entity = registry.async_get_or_create("vacuum", "demo", "first")
    other = registry.async_get_or_create("vacuum", "demo", "second")
    flow = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_ROBOT), context={"source": SOURCE_USER}
    )
    await hass.config_entries.subentries.async_configure(
        flow["flow_id"], {CONF_ROBOT_ENTITY_ID: entity.entity_id}
    )
    subentry = next(iter(entry.subentries.values()))
    flow = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_ROBOT),
        context={"source": SOURCE_RECONFIGURE, "subentry_id": subentry.subentry_id},
    )
    result = await hass.config_entries.subentries.async_configure(
        flow["flow_id"], {CONF_ROBOT_ENTITY_ID: other.entity_id}
    )
    assert result["errors"] == {CONF_ROBOT_ENTITY_ID: "robot_identity_change"}
