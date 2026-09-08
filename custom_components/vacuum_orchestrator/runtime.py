"""Home Assistant composition root for the single orchestrator runtime."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event

from .adapters.home_assistant_vacuum import HomeAssistantVacuumAdapter
from .application.orchestrator import VacuumOrchestrator
from .application.robot_session import RobotOwnershipRegistry
from .const import (
    CONF_ADAPTER,
    CONF_INSTALLATION_ID,
    CONF_LAST_CLEAN_END_ENTITY_ID,
    CONF_LAST_CLEAN_START_ENTITY_ID,
    CONF_ROBOT_ENTITY_ID,
    CONF_ROBOT_REGISTRY_ID,
    CONF_TARGET_AREAS,
    DOMAIN,
    STORE_VERSION,
    SUBENTRY_TYPE_ROBOT,
)
from .domain.errors import ConflictError
from .infrastructure.critical_repository import (
    CriticalOrchestratorRepository,
    MigratingOrchestratorRepository,
)
from .infrastructure.ha_store import HomeAssistantSnapshotBackend
from .ports.robot import RobotAdapter

RUNTIME_KEY = f"{DOMAIN}_runtime"
OWNERSHIP_KEY = f"{DOMAIN}_ownership"


@dataclass(slots=True)
class VacuumOrchestratorRuntime:
    """Loaded resources for the integration-wide queue and robot pool."""

    orchestrator: VacuumOrchestrator
    source_robot_ids: tuple[str, ...]
    unsubscribe_observer: Callable[[], None] | None = None


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
    adapters: dict[str, RobotAdapter] = {}
    source_ids: list[str] = []
    ownership = _ownership_registry(hass)
    try:
        for subentry_id, subentry in entry.subentries.items():
            if subentry.subentry_type != SUBENTRY_TYPE_ROBOT:
                continue
            data: dict[str, Any] = dict(subentry.data)
            registry_id = str(data[CONF_ROBOT_REGISTRY_ID])
            source_robot_id = f"entity_registry:{registry_id}"
            ownership.claim(source_robot_id, installation_id)
            source_ids.append(source_robot_id)
            targets = tuple(str(item) for item in data[CONF_TARGET_AREAS])
            adapters[subentry_id] = HomeAssistantVacuumAdapter(
                hass,
                robot_id=subentry_id,
                source_robot_id=source_robot_id,
                entity_id=str(data[CONF_ROBOT_ENTITY_ID]),
                adapter_name=str(data[CONF_ADAPTER]),
                target_areas=targets,
                last_clean_start_entity_id=(
                    str(data[CONF_LAST_CLEAN_START_ENTITY_ID])
                    if data.get(CONF_LAST_CLEAN_START_ENTITY_ID)
                    else None
                ),
                last_clean_end_entity_id=(
                    str(data[CONF_LAST_CLEAN_END_ENTITY_ID])
                    if data.get(CONF_LAST_CLEAN_END_ENTITY_ID)
                    else None
                ),
            )
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
        )

        def state_reader(references: tuple[str, ...]) -> dict[str, str | None]:
            return {
                reference: (
                    state.state if (state := hass.states.get(reference)) else None
                )
                for reference in references
            }

        orchestrator = VacuumOrchestrator(
            installation_id,
            repository,
            adapters,
            state_reader=state_reader,
        )
        await orchestrator.async_initialize()
    except Exception:
        for source_robot_id in source_ids:
            ownership.release(source_robot_id, installation_id)
        raise

    watched_by_robot: dict[str, set[str]] = {
        robot_id: set(adapter.watched_entity_ids)
        for robot_id, adapter in adapters.items()
        if isinstance(adapter, HomeAssistantVacuumAdapter)
    }
    entity_ids = set().union(*watched_by_robot.values()) if watched_by_robot else set()

    @callback
    def observation_changed(event: Event[Any]) -> None:
        entity_id = str(event.data["entity_id"])
        for robot_id, watched in watched_by_robot.items():
            if entity_id in watched:
                hass.async_create_task(
                    orchestrator.async_process_robot_observation(robot_id),
                    f"{DOMAIN} observation {robot_id}",
                )

    unsubscribe = (
        async_track_state_change_event(hass, entity_ids, observation_changed)
        if entity_ids
        else None
    )
    runtime = VacuumOrchestratorRuntime(orchestrator, tuple(source_ids), unsubscribe)
    async_get_registry(hass)[entry.entry_id] = runtime
    entry.runtime_data = runtime
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def async_unload_orchestrator(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Release runtime resources and stable source-robot claims."""
    runtime = async_get_registry(hass).pop(entry.entry_id, None)
    if runtime is None:
        return True
    if runtime.unsubscribe_observer is not None:
        runtime.unsubscribe_observer()
    installation_id = str(entry.data[CONF_INSTALLATION_ID])
    ownership = _ownership_registry(hass)
    for source_robot_id in runtime.source_robot_ids:
        ownership.release(source_robot_id, installation_id)
    return True


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload runtime composition after robot-profile changes."""
    await hass.config_entries.async_reload(entry.entry_id)
