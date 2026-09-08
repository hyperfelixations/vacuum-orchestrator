"""Compact status sensors for Vacuum Orchestrator."""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .domain.types import JobState
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
        active = {JobState.DISPATCHING, JobState.RUNNING, JobState.CANCELING}
        return sum(
            job.state in active
            for job in self._runtime.orchestrator.state.jobs.values()
        )


class AttentionJobCountSensor(_OrchestratorSensor):
    """Expose unresolved job count."""

    def __init__(self, runtime: VacuumOrchestratorRuntime) -> None:
        super().__init__(runtime, "attention_jobs", "Jobs needing attention")

    @property
    def native_value(self) -> int:
        """Return unresolved job count."""
        return sum(
            job.state is JobState.NEEDS_ATTENTION
            for job in self._runtime.orchestrator.state.jobs.values()
        )
