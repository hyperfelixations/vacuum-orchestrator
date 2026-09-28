"""Optional room release switches using the same commands as public actions."""

from typing import Any, cast

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .domain.releases import ReleaseKind
from .room_entities import RoomEntity, setup_room_entities
from .runtime import VacuumOrchestratorRuntime


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Register disabled-by-default release controls for current and future rooms."""
    runtime = cast(VacuumOrchestratorRuntime, entry.runtime_data)
    setup_room_entities(
        entry,
        runtime,
        async_add_entities,
        lambda room_id: [RoomReleaseSwitch(runtime, room_id, "release")],
    )


class RoomReleaseSwitch(RoomEntity, SwitchEntity):
    """Turning on grants permanent release; turning off revokes future admission."""

    @property
    def is_on(self) -> bool:
        """Show availability for a new job, independently of active admissions."""
        grant = self.room.release
        return bool(grant and grant.allows_new_job(self.now()))

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Grant permanent release through the shared writer."""
        await self.runtime.orchestrator.rooms.async_grant(
            self.room_id, ReleaseKind.PERMANENT
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Revoke future admissions without cancelling ongoing work."""
        await self.runtime.orchestrator.rooms.async_revoke(self.room_id)
