"""Compact attention binary sensor for Vacuum Orchestrator."""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import OrchestratorEntity
from .runtime import VacuumOrchestratorRuntime


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the integration-wide attention indicator."""
    runtime = cast(VacuumOrchestratorRuntime, entry.runtime_data)
    async_add_entities((OrchestratorAttentionSensor(runtime),))


class OrchestratorAttentionSensor(OrchestratorEntity, BinarySensorEntity):
    """Expose whether a person has to act; see dev doc "Aufmerksamkeit"."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "needs_attention"
    _attr_unique_id = f"{DOMAIN}_needs_attention"

    def __init__(self, runtime: VacuumOrchestratorRuntime) -> None:
        self._runtime = runtime
        self._unsubscribe: Callable[[], None] | None = None

    @property
    def is_on(self) -> bool:
        """Return whether a device fault or a recovery needs a person."""
        return bool(self._runtime.orchestrator.attention())

    async def async_added_to_hass(self) -> None:
        """Subscribe after entity registration."""
        await super().async_added_to_hass()
        # Device faults change without a commit.
        self._unsubscribe = self._runtime.orchestrator.subscribe_view(
            self._state_changed
        )

    async def async_will_remove_from_hass(self) -> None:
        """Release the state listener."""
        if self._unsubscribe is not None:
            self._unsubscribe()
        await super().async_will_remove_from_hass()

    @callback
    def _state_changed(self) -> None:
        self.async_write_ha_state()
