"""Home Assistant composition root for the single orchestrator runtime."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er

from .application.orchestrator import VacuumOrchestrator
from .application.robot_session import RobotOwnershipRegistry
from .application.room_service import AreaSnapshot
from .const import CONF_INSTALLATION_ID, DOMAIN, STORE_VERSION
from .domain.errors import ConflictError
from .domain.requirements import StateObservation, StateRequirement
from .infrastructure.critical_repository import (
    CriticalOrchestratorRepository,
    MigratingOrchestratorRepository,
)
from .infrastructure.ha_store import HomeAssistantSnapshotBackend
from .runtime_adapters import build_adapters
from .runtime_controller import RuntimeController

RUNTIME_KEY = f"{DOMAIN}_runtime"
OWNERSHIP_KEY = f"{DOMAIN}_ownership"


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
    installation_id = str(entry.data[CONF_INSTALLATION_ID])
    source_ids: list[str] = []
    ownership = _ownership_registry(hass)
    try:
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
            previous_backend=HomeAssistantSnapshotBackend(
                hass, f"{DOMAIN}.2", store_version=2, store_minor_version=0
            ),
        )

        def state_reader(references: tuple[str, ...]) -> dict[str, str | None]:
            return {
                reference: (
                    state.state if (state := hass.states.get(reference)) else None
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
        )
        await orchestrator.async_initialize()
        await orchestrator.rooms.async_import_areas(
            {
                area.id: AreaSnapshot(area.name, area.floor_id)
                for area in ar.async_get(hass).async_list_areas()
            }
        )
    except Exception:
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
    return True


async def async_unload_orchestrator(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Release runtime resources and stable source-robot claims."""
    runtime = async_get_registry(hass).pop(entry.entry_id, None)
    if runtime is None:
        return True
    if runtime.controller is not None:
        await runtime.controller.async_close()
    return True
