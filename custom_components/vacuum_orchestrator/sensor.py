"""Compact status sensors for Vacuum Orchestrator."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any, cast

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .domain.types import OperationKind
from .room_entities import RoomEntity, setup_room_entities
from .runtime import VacuumOrchestratorRuntime


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up bounded status sensors for the single runtime."""
    runtime = cast(VacuumOrchestratorRuntime, entry.runtime_data)
    async_add_entities(
        (
            QueueModeSensor(runtime),
            QueueLengthSensor(runtime),
            ActiveJobCountSensor(runtime),
            AttentionJobCountSensor(runtime),
        )
    )
    setup_room_entities(
        entry,
        runtime,
        async_add_entities,
        lambda room_id: [
            RoomCleaningSensor(runtime, room_id, operation, measurement)
            for operation in (OperationKind.VACUUM, OperationKind.MOP)
            for measurement in ("last_cleaning", "elapsed_seconds", "due")
        ],
    )


class RoomCleaningSensor(RoomEntity, SensorEntity):
    """Expose effective cleaning, elapsed time and due state with evidence quality."""

    def __init__(
        self,
        runtime: VacuumOrchestratorRuntime,
        room_id: str,
        operation: OperationKind,
        measurement: str,
    ) -> None:
        super().__init__(runtime, room_id, f"{operation.value}_{measurement}")
        self.operation = operation
        self.measurement = measurement
        if measurement == "last_cleaning":
            self._attr_device_class = SensorDeviceClass.TIMESTAMP
        elif measurement == "elapsed_seconds":
            self._attr_device_class = SensorDeviceClass.DURATION
            self._attr_native_unit_of_measurement = "s"

    @property
    def native_value(self) -> datetime | float | str | None:
        """Project state without moving the stored cleaning reference point."""
        stamp = self.room.last_cleaning.get(self.operation)
        if self.measurement == "due":
            return self.room.due(self.operation, self.now()).state.value
        if stamp is None:
            return None
        if self.measurement == "last_cleaning":
            return stamp.completed_at
        return max(0, (self.now() - stamp.completed_at).total_seconds())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Keep inferred quality and last confirmed completion visible."""
        stamp = self.room.last_cleaning.get(self.operation)
        confirmed = self.room.last_confirmed.get(self.operation)
        report = self.room.due(self.operation, self.now())
        return {
            **super().extra_state_attributes,
            "quality": stamp.quality.value if stamp else None,
            "receipt_id": stamp.receipt_id if stamp else None,
            "last_confirmed": confirmed.completed_at.isoformat() if confirmed else None,
            "due_reason": report.reason,
        }


class _OrchestratorSensor(SensorEntity):
    """Subscribe one sensor to verified orchestrator commits."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True

    def __init__(self, runtime: VacuumOrchestratorRuntime, key: str, name: str) -> None:
        self._runtime = runtime
        self._attr_unique_id = f"{DOMAIN}_{key}"
        self._attr_name = name
        self._unsubscribe: Callable[[], None] | None = None

    async def async_added_to_hass(self) -> None:
        """Subscribe after entity registration."""
        await super().async_added_to_hass()
        self._unsubscribe = self._runtime.orchestrator.subscribe(self._state_changed)

    async def async_will_remove_from_hass(self) -> None:
        """Release the state listener."""
        if self._unsubscribe is not None:
            self._unsubscribe()
        await super().async_will_remove_from_hass()

    @callback
    def _state_changed(self) -> None:
        self.async_write_ha_state()


class QueueModeSensor(_OrchestratorSensor):
    """Expose the persistent automatic queue mode."""

    def __init__(self, runtime: VacuumOrchestratorRuntime) -> None:
        super().__init__(runtime, "queue_mode", "Queue mode")

    @property
    def native_value(self) -> str:
        """Return current queue mode."""
        return self._runtime.orchestrator.state.mode.value


class QueueLengthSensor(_OrchestratorSensor):
    """Expose the bounded count of pending jobs."""

    def __init__(self, runtime: VacuumOrchestratorRuntime) -> None:
        super().__init__(runtime, "queue_length", "Queue length")

    @property
    def native_value(self) -> int:
        """Return pending queue length."""
        return len(self._runtime.orchestrator.state.queue)


class ActiveJobCountSensor(_OrchestratorSensor):
    """Expose active job count without embedding job data in attributes."""

    def __init__(self, runtime: VacuumOrchestratorRuntime) -> None:
        super().__init__(runtime, "active_jobs", "Active jobs")

    @property
    def native_value(self) -> int:
        """Return active job count."""
        return self._runtime.orchestrator.state.active_job_count


class AttentionJobCountSensor(_OrchestratorSensor):
    """Expose unresolved job count."""

    def __init__(self, runtime: VacuumOrchestratorRuntime) -> None:
        super().__init__(runtime, "attention_jobs", "Jobs needing attention")

    @property
    def native_value(self) -> int:
        """Return unresolved job count."""
        return self._runtime.orchestrator.state.attention_job_count
