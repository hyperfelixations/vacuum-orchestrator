"""The service device and naming shared by all Vacuum Orchestrator entities."""

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN

# One instance per HA (`single_config_entry`); see dev doc "Entitäten".
SERVICE_DEVICE = DeviceInfo(
    identifiers={(DOMAIN, DOMAIN)},
    entry_type=DeviceEntryType.SERVICE,
    translation_key="orchestrator",
)


class OrchestratorEntity(Entity):
    """An entity of the service device, named by its translation key."""

    _attr_device_info = SERVICE_DEVICE
    _attr_has_entity_name = True
    _attr_should_poll = False
