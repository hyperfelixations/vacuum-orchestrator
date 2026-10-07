"""Entity-registry references that survive an entity ID rename."""

from homeassistant.core import HomeAssistant, valid_entity_id
from homeassistant.helpers import entity_registry as er


class RegistryEntityReferences:
    """Store a registered entity by its registry ID, as HA automations do."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._registry = er.async_get(hass)

    def reference(self, entity_id: str) -> str:
        """Return the registry ID, or the entity ID of an unregistered entity."""
        entry = self._registry.async_get(entity_id)
        return entry.id if entry is not None else entity_id

    def entity_id(self, reference: str) -> str | None:
        """Return the current entity ID; None when removed or disabled."""
        if valid_entity_id(reference):
            return reference
        entry = self._registry.async_get(reference)
        return entry.entity_id if entry is not None and not entry.disabled else None
