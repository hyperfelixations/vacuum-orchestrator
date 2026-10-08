"""Home Assistant composition root for the single orchestrator runtime."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .adapters.entity_references import RegistryEntityReferences
from .application.orchestrator import VacuumOrchestrator
from .application.robot_session import RobotOwnershipRegistry
from .application.room_service import AreaSnapshot
from .application.tracing import TraceEvent, TraceRecorder
from .const import CONF_INSTALLATION_ID, DOMAIN, SIGNAL_VIEW_CHANGED, STORE_VERSION
from .domain.errors import ConflictError
from .domain.requirements import StateObservation, StateRequirement
from .infrastructure.codec import (
    migrate_schema_four,
    migrate_schema_three,
    migrate_schema_two,
)
from .infrastructure.critical_repository import (
    CriticalOrchestratorRepository,
    MigratingOrchestratorRepository,
)
from .infrastructure.ha_store import HomeAssistantSnapshotBackend
from .infrastructure.telemetry import LoggingSink
from .runtime_adapters import build_adapters
from .runtime_controller import RuntimeController

RUNTIME_KEY = f"{DOMAIN}_runtime"
OWNERSHIP_KEY = f"{DOMAIN}_ownership"
TELEMETRY_KEY = f"{DOMAIN}_telemetry"


@dataclass(slots=True)
class VacuumOrchestratorRuntime:
    """Loaded resources for the integration-wide queue and robot pool."""

    orchestrator: VacuumOrchestrator
    source_robot_ids: tuple[str, ...]
    controller: RuntimeController | None = None


def async_get_registry(hass: HomeAssistant) -> dict[str, VacuumOrchestratorRuntime]:
    """Return the process-local config-entry runtime registry."""
    return cast(
        dict[str, VacuumOrchestratorRuntime], hass.data.setdefault(RUNTIME_KEY, {})
    )


def async_get_runtime(hass: HomeAssistant) -> VacuumOrchestratorRuntime:
    """Resolve the single loaded runtime without a user-facing entry id."""
    runtimes = async_get_registry(hass)
    if not runtimes:
        raise ConflictError("orchestrator_not_loaded")
    if len(runtimes) != 1:
        raise ConflictError("multiple_orchestrator_entries_loaded")
    return next(iter(runtimes.values()))


def _ownership_registry(hass: HomeAssistant) -> RobotOwnershipRegistry:
    return cast(
        RobotOwnershipRegistry,
        hass.data.setdefault(OWNERSHIP_KEY, RobotOwnershipRegistry()),
    )


async def async_setup_orchestrator(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Compose robot adapters and initialize the one global state owner."""
    source_ids: list[str] = []
    ownership = _ownership_registry(hass)
    trace = TraceRecorder(sink=LoggingSink())
    hass.data[TELEMETRY_KEY] = {entry.entry_id: trace}
    trace.record(TraceEvent.LIFECYCLE, datetime.now(UTC), stage="starting")
    try:
        installation_id = str(entry.data[CONF_INSTALLATION_ID])
        adapters, aliases = build_adapters(
            hass, entry, lambda: orchestrator.state.room_registry.rooms
        )
        for source_id in set().union(*aliases.values()) if aliases else ():
            ownership.claim(source_id, installation_id)
            source_ids.append(source_id)
        repository = MigratingOrchestratorRepository(
            CriticalOrchestratorRepository(
                HomeAssistantSnapshotBackend(hass, f"{DOMAIN}.{STORE_VERSION}")
            ),
            HomeAssistantSnapshotBackend(
                hass,
                f"{DOMAIN}.1.{entry.entry_id}",
                store_version=1,
                store_minor_version=1,
            ),
            installation_id,
            previous_stores=(
                (
                    HomeAssistantSnapshotBackend(
                        hass, f"{DOMAIN}.4", store_version=4, store_minor_version=0
                    ),
                    migrate_schema_four,
                ),
                (
                    HomeAssistantSnapshotBackend(
                        hass, f"{DOMAIN}.3", store_version=3, store_minor_version=0
                    ),
                    migrate_schema_three,
                ),
                (
                    HomeAssistantSnapshotBackend(
                        hass, f"{DOMAIN}.2", store_version=2, store_minor_version=0
                    ),
                    migrate_schema_two,
                ),
            ),
        )

        entity_references = RegistryEntityReferences(hass)

        def state_reader(references: tuple[str, ...]) -> dict[str, str | None]:
            return {
                reference: (
                    state.state
                    if (entity_id := entity_references.entity_id(reference))
                    and (state := hass.states.get(entity_id))
                    else None
                )
                for reference in references
            }

        def requirement_reader(
            requirements: tuple[StateRequirement, ...],
        ) -> dict[str, StateObservation]:
            observations = {}
            registry = er.async_get(hass)
            for requirement in requirements:
                entity_id = requirement.entity_id
                if requirement.entity_registry_id is not None:
                    binding = registry.async_get(requirement.entity_registry_id)
                    entity_id = (
                        binding.entity_id
                        if binding is not None and not binding.disabled
                        else ""
                    )
                value = hass.states.get(entity_id) if entity_id else None
                observations[requirement.entity_id] = StateObservation(
                    value.state if value else None,
                    value.last_reported if value else None,
                )
            return observations

        orchestrator = VacuumOrchestrator(
            installation_id,
            repository,
            adapters,
            state_reader=state_reader,
            requirement_reader=requirement_reader,
            entity_references=entity_references,
            trace=trace,
        )
        await orchestrator.async_initialize()
        await orchestrator.rooms.async_import_areas(
            {
                area.id: AreaSnapshot(area.name, area.floor_id)
                for area in ar.async_get(hass).async_list_areas()
            }
        )
    except Exception as err:
        trace.record(TraceEvent.LIFECYCLE, datetime.now(UTC), stage="failed", error=err)
        for source_robot_id in source_ids:
            ownership.release(source_robot_id, installation_id)
        raise

    controller = RuntimeController(
        hass, entry, orchestrator, ownership, installation_id, aliases
    )
    runtime = VacuumOrchestratorRuntime(orchestrator, tuple(source_ids), controller)
    async_get_registry(hass)[entry.entry_id] = runtime
    entry.runtime_data = runtime
    controller.start()
    trace.record(TraceEvent.LIFECYCLE, datetime.now(UTC), stage="ready")
    return True


async def async_unload_orchestrator(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Release runtime resources and stable source-robot claims."""
    runtime = async_get_registry(hass).pop(entry.entry_id, None)
    if runtime is None:
        return True
    runtime.orchestrator.trace.record(
        TraceEvent.LIFECYCLE, datetime.now(UTC), stage="closing"
    )
    if runtime.controller is not None:
        await runtime.controller.async_close()
    runtime.orchestrator.trace.record(
        TraceEvent.LIFECYCLE, datetime.now(UTC), stage="closed"
    )
    async_dispatcher_send(hass, SIGNAL_VIEW_CHANGED)
    return True
