"""HA event lifecycle, discovery reconciliation and scheduler wakeup wiring."""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, EVENT_STATE_CHANGED
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .adapters.discovery import discover_robots, resolve_entity_id
from .adapters.home_assistant_vacuum import HomeAssistantVacuumAdapter
from .adapters.roborock import RoborockAdapter
from .application.orchestrator import VacuumOrchestrator
from .application.robot_session import RobotOwnershipRegistry
from .application.room_service import AreaSnapshot
from .application.scheduler import WakeupScheduler
from .application.tracing import TraceEvent
from .configuration import validate_robot_configuration
from .const import (
    API_VERSION,
    CONF_ROBOT_ENTITY_ID,
    CONF_ROBOT_REGISTRY_ID,
    DOMAIN,
    SUBENTRY_TYPE_ROBOT,
)
from .domain.due import DueBasis, DueState
from .domain.errors import ConflictError, OrchestratorError
from .domain.monitoring import next_deadline
from .domain.types import OperationKind
from .repairs import RepairReporter
from .runtime_adapters import build_adapters


class RuntimeController:
    """Own event subscriptions, task draining and identity claims for one entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        orchestrator: VacuumOrchestrator,
        ownership: RobotOwnershipRegistry,
        installation_id: str,
        aliases: dict[str, frozenset[str]],
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.orchestrator = orchestrator
        self.ownership = ownership
        self.installation_id = installation_id
        self.aliases = aliases
        self._unsubscribers: list[Callable[[], None]] = []
        self._registry_dirty = True
        self._changed: set[str] = set()
        self._watched: set[str] = set()
        self._known_profiles = {
            key: str(value.data[CONF_ROBOT_REGISTRY_ID])
            for key, value in entry.subentries.items()
            if value.subentry_type == SUBENTRY_TYPE_ROBOT
        }
        self._active_ids: frozenset[str] = frozenset()
        self._occupancy_sources: dict[str, str | None] = {}
        self._occupancy_dirty: set[str] = set()
        self._restart = True
        self._closed = False
        self.last_error: str | None = None
        self.repairs = RepairReporter(hass, orchestrator)
        self.scheduler = WakeupScheduler(
            self._async_work,
            self._next_deadline,
            lambda: datetime.now(UTC),
            lambda coro: hass.async_create_task(
                coro, f"{DOMAIN} scheduler", eager_start=False
            ),
            self._error,
        )

    def start(self) -> None:
        """Subscribe only after the verified runtime has been published."""
        self._unsubscribers.extend(
            [
                self.hass.bus.async_listen(EVENT_STATE_CHANGED, self._state_changed),
                self.hass.bus.async_listen_once(
                    EVENT_HOMEASSISTANT_STOP, self._stopping
                ),
                self.hass.bus.async_listen(
                    ar.EVENT_AREA_REGISTRY_UPDATED, self._registry_changed
                ),
                self.hass.bus.async_listen(
                    er.EVENT_ENTITY_REGISTRY_UPDATED, self._registry_changed
                ),
                self.hass.bus.async_listen(
                    dr.EVENT_DEVICE_REGISTRY_UPDATED, self._registry_changed
                ),
                self.orchestrator.subscribe(self.scheduler.notify),
                self.orchestrator.subscribe_view(self._publish_change),
                self.entry.add_update_listener(self._entry_updated),
            ]
        )
        self.scheduler.notify()

    async def _stopping(self, _event: Event[Any]) -> None:
        await self.async_close()

    @callback
    def _publish_change(self) -> None:
        self.hass.bus.async_fire(
            f"{DOMAIN}_state_changed",
            {
                "api_version": API_VERSION,
                "runtime_id": self.orchestrator.runtime_id,
                "runtime_sequence": self.orchestrator.runtime_sequence,
                "commit_id": self.orchestrator.state.commit_id,
            },
        )

    @callback
    def _state_changed(self, event: Event[Any]) -> None:
        entity_id = str(event.data["entity_id"])
        if entity_id in self._watched:
            self._changed.add(entity_id)
            self.scheduler.notify()

    @callback
    def _registry_changed(self, _event: Event[Any]) -> None:
        self._registry_dirty = True
        self.scheduler.notify()

    async def _entry_updated(self, _hass: HomeAssistant, _entry: ConfigEntry) -> None:
        self._registry_dirty = True
        self.scheduler.notify()

    def _error(self, err: Exception) -> None:
        self.last_error = (
            err.code
            if isinstance(err, OrchestratorError)
            else "runtime_reconciliation_failed"
        )
        self.orchestrator.trace.record(
            TraceEvent.ERROR, datetime.now(UTC), reason=self.last_error, error=err
        )

    async def _async_work(self) -> None:
        active = frozenset(
            lease.robot_id for lease in self.orchestrator.state.robot_leases.values()
        )
        rebuild = self._registry_dirty or active != self._active_ids
        self._registry_dirty = False
        if rebuild:
            try:
                await self._async_discover()
                await self.orchestrator.rooms.async_import_areas(
                    {
                        area.id: AreaSnapshot(area.name, area.floor_id)
                        for area in ar.async_get(self.hass).async_list_areas()
                    }
                )
                await self._async_rebuild_adapters(active)
            except Exception:
                self._registry_dirty = True
                raise
            self._active_ids = active
        self._refresh_watched_entities()
        changed, self._changed = self._changed, set()
        now = datetime.now(UTC)
        values = {
            source: (
                state.state
                if entity_id and (state := self.hass.states.get(entity_id))
                else None
            )
            for source, entity_id in self._occupancy_sources.items()
            if self._restart
            or rebuild
            or source in self._occupancy_dirty
            or entity_id in changed
        }
        if values:
            await self.orchestrator.rooms.async_observe_occupancy(
                values, now, restart=self._restart
            )
        self._restart = False
        self._occupancy_dirty.clear()
        self.orchestrator.notify_runtime_change()
        for robot_id, adapter in self.orchestrator.adapters.items():
            lease = self.orchestrator.state.robot_leases.get(
                adapter.profile.source_robot_id
            )
            deadline = (
                next_deadline(self.orchestrator.state.attempts[lease.attempt_id])
                if lease
                else None
            )
            watched = (
                adapter.watched_entity_ids
                if isinstance(adapter, HomeAssistantVacuumAdapter)
                else ()
            )
            if (
                rebuild
                or set(watched) & changed
                or (deadline is not None and deadline <= now)
            ):
                await self.orchestrator.async_process_robot_observation(robot_id)
        await self.orchestrator.templates.async_generate_due()
        await self.orchestrator.async_dispatch_available()
        await self.orchestrator.async_reconcile_queue_run()
        self.repairs.update()
        self.last_error = None

    async def _async_discover(self) -> None:
        current = {
            key: str(value.data[CONF_ROBOT_REGISTRY_ID])
            for key, value in self.entry.subentries.items()
            if value.subentry_type == SUBENTRY_TYPE_ROBOT
        }
        excluded = set(self.entry.data.get("excluded_robot_registry_ids", ()))
        removed = {
            value for key, value in self._known_profiles.items() if key not in current
        }
        if removed - excluded:
            excluded.update(removed)
            self.hass.config_entries.async_update_entry(
                self.entry,
                data={
                    **self.entry.data,
                    "excluded_robot_registry_ids": sorted(excluded),
                },
            )
        if self.entry.data.get("auto_discover_robots", True):
            for candidate in discover_robots(self.hass):
                if (
                    candidate.registry_id in current.values()
                    or candidate.registry_id in excluded
                    or len(current) >= 20
                ):
                    continue
                try:
                    data = validate_robot_configuration(
                        self.hass,
                        self.entry,
                        {CONF_ROBOT_ENTITY_ID: candidate.entity_id},
                    )
                except OrchestratorError:
                    continue
                subentry = ConfigSubentry(
                    data=MappingProxyType(data),
                    subentry_type=SUBENTRY_TYPE_ROBOT,
                    title=candidate.name,
                    unique_id=candidate.registry_id,
                )
                self.hass.config_entries.async_add_subentry(self.entry, subentry)
                current[subentry.subentry_id] = candidate.registry_id
        self._known_profiles = current

    async def _async_rebuild_adapters(self, active: frozenset[str]) -> None:
        adapters, aliases = build_adapters(
            self.hass, self.entry, lambda: self.orchestrator.state.room_registry.rooms
        )
        for robot_id in active:
            retained = self.aliases.get(robot_id, frozenset())
            if any(
                retained & values for key, values in aliases.items() if key != robot_id
            ):
                raise ConflictError("duplicate_active_physical_robot")
            if retained:
                aliases[robot_id] = retained
        old_claims = set().union(*self.aliases.values()) if self.aliases else set()
        new_claims = set().union(*aliases.values()) if aliases else set()
        acquired: set[str] = set()
        try:
            for source_id in new_claims - old_claims:
                self.ownership.claim(source_id, self.installation_id)
                acquired.add(source_id)
            await self.orchestrator.async_replace_adapters(adapters)
        except Exception:
            for source_id in acquired:
                self.ownership.release(source_id, self.installation_id)
            raise
        for source_id in old_claims - new_claims:
            self.ownership.release(source_id, self.installation_id)
        self.aliases = aliases
        await asyncio.gather(
            *(
                self._async_prepare_maps(adapter)
                for robot_id, adapter in self.orchestrator.adapters.items()
                if robot_id not in active and isinstance(adapter, RoborockAdapter)
            )
        )

    async def _async_prepare_maps(self, adapter: RoborockAdapter) -> None:
        try:
            async with asyncio.timeout(10):
                await adapter.async_refresh_maps()
        except Exception:
            self.orchestrator.trace.record(
                TraceEvent.BLOCKED,
                datetime.now(UTC),
                robot_id=adapter.profile.robot_id,
                reason="map_inventory_unavailable",
            )

    def _refresh_watched_entities(self) -> None:
        watched: set[str] = set()
        occupancy: dict[str, str | None] = {}
        for adapter in self.orchestrator.adapters.values():
            if isinstance(adapter, HomeAssistantVacuumAdapter):
                watched.update(adapter.watched_entity_ids)
            for requirement in adapter.profile.requirements:
                reference = (
                    resolve_entity_id(self.hass, requirement.entity_registry_id)
                    if requirement.entity_registry_id
                    else requirement.entity_id
                )
                if reference:
                    watched.add(reference)
        for job in self.orchestrator.state.jobs.values():
            watched.update((*job.intent.required_on, *job.intent.required_off))
        for room in self.orchestrator.rooms.registry.rooms.values():
            for requirement in room.requirements:
                entity_id = (
                    resolve_entity_id(self.hass, requirement.entity_registry_id)
                    if requirement.entity_registry_id
                    else requirement.entity_id
                )
                if entity_id:
                    watched.add(entity_id)
            policy = room.due_policy
            if policy.occupancy_entity_id:
                entity_id = (
                    resolve_entity_id(self.hass, policy.occupancy_entity_registry_id)
                    if policy.occupancy_entity_registry_id
                    else policy.occupancy_entity_id
                )
                occupancy[policy.occupancy_entity_id] = entity_id
                if entity_id:
                    watched.add(entity_id)
        self._occupancy_dirty.update(
            source
            for source, entity_id in occupancy.items()
            if source not in self._occupancy_sources
            or self._occupancy_sources[source] != entity_id
        )
        self._occupancy_sources = occupancy
        self._watched = watched

    def _next_deadline(self) -> datetime | None:
        now = datetime.now(UTC)
        deadlines = [
            value
            for lease in self.orchestrator.state.robot_leases.values()
            if (
                value := next_deadline(
                    self.orchestrator.state.attempts[lease.attempt_id]
                )
            )
            is not None
        ]
        run = self.orchestrator.state.queue_run
        if run is not None and run.deadline is not None:
            deadlines.append(run.deadline)
        requirements = [
            requirement
            for adapter in self.orchestrator.adapters.values()
            for requirement in adapter.profile.requirements
        ]
        for room in self.orchestrator.rooms.registry.rooms.values():
            requirements.extend(room.requirements)
            if (
                room.release
                and room.release.expires_at
                and room.release.expires_at > now
            ):
                deadlines.append(room.release.expires_at)
            for operation in (OperationKind.VACUUM, OperationKind.MOP):
                report = room.due(operation, now)
                if report.state is not DueState.FRESH:
                    continue
                if report.due_at and report.due_at > now:
                    deadlines.append(report.due_at)
                elif (
                    room.due_policy.basis is DueBasis.OCCUPIED
                    and room.occupancy.occupied is True
                    and report.remaining_seconds is not None
                    and report.remaining_seconds > 0
                ):
                    deadlines.append(now + timedelta(seconds=report.remaining_seconds))
        for requirement in requirements:
            if requirement.max_age_seconds is None:
                continue
            entity_id = (
                resolve_entity_id(self.hass, requirement.entity_registry_id)
                if requirement.entity_registry_id
                else requirement.entity_id
            )
            observed = self.hass.states.get(entity_id) if entity_id else None
            if observed:
                expiry = observed.last_reported + timedelta(
                    seconds=requirement.max_age_seconds, microseconds=1
                )
                if expiry > now:
                    deadlines.append(expiry)
        return min(deadlines) if deadlines else None

    async def async_close(self) -> None:
        """Unsubscribe, fence execution and drain tasks before releasing claims."""
        if self._closed:
            return
        self._closed = True
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers.clear()
        self.repairs.close()
        try:
            try:
                await self.orchestrator.rooms.async_observe_occupancy(
                    {
                        source: state.state
                        if entity_id and (state := self.hass.states.get(entity_id))
                        else None
                        for source, entity_id in self._occupancy_sources.items()
                    },
                    datetime.now(UTC),
                )
            finally:
                await self.orchestrator.async_shutdown()
        finally:
            await self.scheduler.async_close()
            for source_id in (
                set().union(*self.aliases.values()) if self.aliases else ()
            ):
                self.ownership.release(source_id, self.installation_id)
            self.aliases = {}
