"""Optional room entities derive all state from the authoritative room registry."""

from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.typing import UNDEFINED, UndefinedType

from .const import DOMAIN
from .domain.rooms import Room
from .entity import OrchestratorEntity
from .runtime import VacuumOrchestratorRuntime


def setup_room_entities(
    entry: ConfigEntry,
    runtime: VacuumOrchestratorRuntime,
    add_entities: AddEntitiesCallback,
    factory: Callable[[str], Iterable[Entity]],
) -> None:
    """Register new rooms once while preserving disabled entity registry choices."""
    known: set[str] = set()

    @callback
    def reconcile() -> None:
        created: list[Entity] = []
        for room_id in runtime.orchestrator.rooms.registry.rooms:
            if room_id not in known:
                known.add(room_id)
                created.extend(factory(room_id))
        if created:
            add_entities(created)

    entry.async_on_unload(runtime.orchestrator.subscribe_view(reconcile))
    reconcile()


class RoomEntity(OrchestratorEntity):
    """Stable room identity and event-driven read model with no duplicate storage."""

    _attr_entity_registry_enabled_default = False

    def __init__(
        self, runtime: VacuumOrchestratorRuntime, room_id: str, key: str
    ) -> None:
        self.runtime = runtime
        self.room_id = room_id
        self.key = key
        self._attr_unique_id = f"{DOMAIN}_room_{room_id}_{key}"
        self._attr_translation_key = key

    @property
    def room(self) -> Room:
        """Return the authoritative current room."""
        return self.runtime.orchestrator.rooms.registry.resolve(self.room_id)

    @property
    def name(self) -> str | UndefinedType | None:
        """Fill the current room name into the translated name on every write."""
        key = self._name_translation_key if self.platform_data else None
        template = key and self.platform_data.platform_translations.get(key)
        return template.format(room=self.room.name) if template else UNDEFINED

    @property
    def available(self) -> bool:
        """Expose exclusions and missing area references without deleting history."""
        return self.room.enabled and not self.room.area_missing

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Keep attributes compact and exclude global job or history lists."""
        return {"room_id": self.room_id}

    async def async_added_to_hass(self) -> None:
        """Subscribe after registration; clock refreshes do not write storage."""
        await super().async_added_to_hass()
        self.async_on_remove(self.runtime.orchestrator.subscribe_view(self._changed))
        self.async_on_remove(
            async_track_time_interval(
                self.hass,
                self._tick,
                timedelta(minutes=1),
            )
        )

    @callback
    def _changed(self) -> None:
        self.async_write_ha_state()

    @callback
    def _tick(self, _now: datetime) -> None:
        self._changed()

    @staticmethod
    def now() -> datetime:
        """Read UTC for point-in-time projections."""
        return datetime.now(UTC)
