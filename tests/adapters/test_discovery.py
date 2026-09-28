"""Robot discovery uses registry identities, never localized entity names."""

from types import MappingProxyType

import pytest
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vacuum_orchestrator.adapters.discovery import (
    candidate_for,
    discover_robots,
    resolve_entity_id,
)
from custom_components.vacuum_orchestrator.configuration import (
    validate_robot_configuration,
)
from custom_components.vacuum_orchestrator.const import DOMAIN
from custom_components.vacuum_orchestrator.domain.errors import (
    ConflictError,
    ValidationError,
)


def test_roborock_dock_roles_are_isolated_per_device(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain="roborock")
    entry.add_to_hass(hass)
    devices = dr.async_get(hass)
    registry = er.async_get(hass)
    robot = devices.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("roborock", "unit-a")}
    )
    dock = devices.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("roborock", "unit-a_dock")}
    )
    other_dock = devices.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("roborock", "unit-b_dock")}
    )
    vacuum = registry.async_get_or_create(
        "vacuum", "roborock", "robot-a", config_entry=entry, device_id=robot.id
    )
    battery = registry.async_get_or_create(
        "sensor",
        "roborock",
        "battery-a",
        config_entry=entry,
        device_id=robot.id,
        original_device_class="battery",
    )
    dock_error = registry.async_get_or_create(
        "sensor",
        "roborock",
        "error-a",
        config_entry=entry,
        device_id=dock.id,
        translation_key="dock_error",
    )
    registry.async_get_or_create(
        "sensor",
        "roborock",
        "error-b",
        config_entry=entry,
        device_id=other_dock.id,
        translation_key="dock_error",
    )
    for role in ("selected_map", "mop_mode"):
        registry.async_get_or_create(
            "select",
            "roborock",
            role,
            config_entry=entry,
            device_id=robot.id,
            translation_key=role,
        )
    registry.async_update_entity_options(
        vacuum.entity_id, "vacuum", {"area_mapping": {"kitchen": ["0_16", "0_17"]}}
    )
    candidate = candidate_for(hass, vacuum.id)
    assert candidate.roles["battery"] == battery.id
    assert candidate.roles["dock_error"] == dock_error.id
    assert candidate.area_targets == {"kitchen": ("0_16", "0_17")}
    assert candidate.protocol == "roborock_v1"
    registry.async_update_entity(vacuum.entity_id, new_entity_id="vacuum.renamed")
    assert resolve_entity_id(hass, vacuum.id) == "vacuum.renamed"
    assert candidate_for(hass, vacuum.id).source_robot_id == candidate.source_robot_id


def test_ambiguous_roles_are_not_chosen_and_missing_registry_has_no_binding(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(domain="roborock")
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("roborock", "unit")}
    )
    registry = er.async_get(hass)
    vacuum = registry.async_get_or_create(
        "vacuum", "roborock", "unit", device_id=device.id, config_entry=entry
    )
    for unique in ("one", "two"):
        registry.async_get_or_create(
            "select",
            "roborock",
            unique,
            config_entry=entry,
            device_id=device.id,
            translation_key="cleaning_mode",
        )
    result = candidate_for(hass, vacuum.id)
    assert "cleaning_mode" not in result.roles
    assert len(result.ambiguous_roles["cleaning_mode"]) == 2
    assert resolve_entity_id(hass, "missing") is None
    with pytest.raises(ValidationError, match="entity_not_registered"):
        candidate_for(hass, "vacuum.missing")
    registry.async_update_entity(
        vacuum.entity_id, disabled_by=er.RegistryEntryDisabler.USER
    )
    assert discover_robots(hass) == ()
    assert resolve_entity_id(hass, vacuum.id) is None


def test_generic_vacuum_accepts_manual_roles_without_claiming_a_mode(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    vacuum = registry.async_get_or_create("vacuum", "demo", "robot")
    mode = registry.async_get_or_create("select", "demo", "mode")
    data = validate_robot_configuration(
        hass,
        entry,
        {
            "robot_entity_id": vacuum.entity_id,
            "roles": {"cleaning_mode": mode.entity_id},
            "mode_options": {"vacuum": "Sweep"},
        },
    )
    assert data["adapter"] == "home_assistant"
    assert data["roles"] == {"cleaning_mode": mode.id}
    assert data["fixed_mode"] is None
    assert data["protocol"] is None
    assert data["mode_options"] == {"vacuum": "Sweep"}


@pytest.mark.parametrize(
    "settings",
    [
        {"roles": {"unknown": "sensor.any"}},
        {"roles": "invalid"},
        {"target_areas": "not-a-list"},
        {"target_areas": [42]},
        {"allowed_operations": "vacuum"},
        {"allowed_operations": ["invalid"]},
        {"enabled": "yes"},
        {"fixed_mode": "smart"},
        {"mode_options": {"vacuum": "same", "mop": "same"}},
        {"vacuum_levels": {"invalid": "balanced"}},
        {"start_timeout_seconds": "invalid"},
        {"start_timeout_seconds": 90000},
        {"start_timeout_seconds": float("nan")},
        {"roles": {"cleaning_mode": "sensor.missing"}},
        {"allowed_operations": []},
        {"protocol": "roborock_v1"},
        {"preference": True},
        {"preference": 101},
        {"minimum_battery": -1},
        {"mode_options": {"vacuum": 42}},
    ],
)
def test_manual_configuration_rejects_invalid_values(
    hass: HomeAssistant, settings: dict
) -> None:
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    vacuum = er.async_get(hass).async_get_or_create("vacuum", "demo", "robot")
    with pytest.raises(ValidationError):
        validate_robot_configuration(
            hass, entry, {"robot_entity_id": vacuum.entity_id, **settings}
        )


def test_duplicate_robot_identity_and_explicit_alias_are_rejected(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    first = registry.async_get_or_create("vacuum", "demo", "first")
    second = registry.async_get_or_create("vacuum", "demo", "second")
    data = validate_robot_configuration(
        hass,
        entry,
        {
            "robot_entity_id": first.entity_id,
            "physical_robot_id": "shared",
            "roles": {"battery": None},
        },
    )
    subentry = ConfigSubentry(
        data=MappingProxyType(data),
        subentry_type="robot",
        title="Robot",
        unique_id=first.id,
    )
    hass.config_entries.async_add_subentry(entry, subentry)
    with pytest.raises(ConflictError, match="already_configured"):
        validate_robot_configuration(hass, entry, {"robot_entity_id": first.entity_id})
    with pytest.raises(ConflictError, match="already_configured"):
        validate_robot_configuration(
            hass,
            entry,
            {"robot_entity_id": second.entity_id, "physical_robot_id": "shared"},
        )
    updated = validate_robot_configuration(
        hass, entry, data, robot_id=subentry.subentry_id
    )
    assert updated["source_robot_id"] == "configured:shared"
    assert updated["roles"]["battery"] is None


def test_legacy_history_binding_and_matter_mode_discovery(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain="matter")
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("matter", "unit")},
        connections={(dr.CONNECTION_NETWORK_MAC, "02:00:00:00:00:01")},
    )
    registry = er.async_get(hass)
    vacuum = registry.async_get_or_create(
        "vacuum", "matter", "robot", device_id=device.id, config_entry=entry
    )
    mode = registry.async_get_or_create(
        "select",
        "matter",
        "mode",
        device_id=device.id,
        config_entry=entry,
        translation_key="clean_mode",
    )
    history = registry.async_get_or_create("sensor", "matter", "history")
    candidate = candidate_for(hass, vacuum.id)
    assert candidate.source_robot_id.startswith("physical:")
    assert candidate.roles["cleaning_mode"] == mode.id
    data = validate_robot_configuration(
        hass,
        MockConfigEntry(domain=DOMAIN),
        {
            "robot_entity_id": vacuum.entity_id,
            "last_clean_start_entity_id": history.entity_id,
        },
    )
    assert data["roles"]["last_clean_start"] == history.id
