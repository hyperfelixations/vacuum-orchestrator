"""Stored entity references follow the entity registry like HA automations do."""

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.vacuum_orchestrator.adapters.entity_references import (
    RegistryEntityReferences,
)
from custom_components.vacuum_orchestrator.api.presentation import (
    present_references,
)


def test_a_registered_entity_is_stored_by_registry_id(hass: HomeAssistant) -> None:
    registry = er.async_get(hass)
    door = registry.async_get_or_create("binary_sensor", "demo", "door")
    references = RegistryEntityReferences(hass)

    reference = references.reference(door.entity_id)
    assert reference == door.id
    registry.async_update_entity(door.entity_id, new_entity_id="binary_sensor.front")
    assert references.entity_id(reference) == "binary_sensor.front"

    registry.async_update_entity(
        "binary_sensor.front", disabled_by=er.RegistryEntryDisabler.USER
    )
    assert references.entity_id(reference) is None
    assert present_references((reference,), references) == [reference]
    registry.async_remove("binary_sensor.front")
    assert references.entity_id(reference) is None


def test_an_unregistered_entity_is_stored_by_entity_id(hass: HomeAssistant) -> None:
    references = RegistryEntityReferences(hass)
    assert references.reference("input_boolean.away") == "input_boolean.away"
    assert references.entity_id("input_boolean.away") == "input_boolean.away"
