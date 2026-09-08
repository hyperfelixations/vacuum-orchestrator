"""Tests for single-entry setup and Roborock subentries."""

from copy import deepcopy

import pytest
from homeassistant.config_entries import SOURCE_USER
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
        "vacuum", "demo", "serial", suggested_object_id="wrong"
    )
    for entity_id, expected in (
        (wrong.entity_id, "not_roborock_entity"),
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
    assert legacy.minor_version == 0
    assert legacy.title == "Vacuum Orchestrator"
    assert legacy.data == {CONF_INSTALLATION_ID: DOMAIN}

    unsupported = MockConfigEntry(domain=DOMAIN, data={}, version=99)
    assert not await async_migrate_entry(hass, unsupported)
