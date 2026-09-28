"""Validated room commands using the orchestrator's single commit path."""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from ..domain.completion import CleaningReceipt
from ..domain.due import DuePolicy, OccupancyCounter
from ..domain.errors import ConflictError, ValidationError
from ..domain.queue import OrchestratorState
from ..domain.releases import ReleaseKind, RoomRelease
from ..domain.room_registry import RoomRegistry
from ..domain.rooms import Room
from ..domain.types import JobState
from ..domain.validation import seconds

Mutation = Callable[[OrchestratorState], OrchestratorState]
Commit = Callable[[Mutation], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class AreaSnapshot:
    """External area metadata without manufacturer targeting assumptions."""

    name: str
    floor_id: str | None = None


class RoomService:
    """Room configuration, grants and observation commands share one writer."""

    def __init__(
        self,
        mutate: Commit,
        state: Callable[[], OrchestratorState],
        clock: Callable[[], datetime],
        id_factory: Callable[[], str],
    ) -> None:
        self._mutate = mutate
        self._state = state
        self._clock = clock
        self._id_factory = id_factory

    @property
    def registry(self) -> RoomRegistry:
        """Return the current verified room snapshot."""
        return self._state().room_registry

    async def async_create(self, name: str, *, area_id: str | None = None) -> str:
        """Create an unreleased canonical room with an optional HA area alias."""
        room = Room(self._id_factory(), name, area_id=area_id)

        def create(state: OrchestratorState) -> OrchestratorState:
            if room.room_id in state.room_registry.rooms:
                raise ConflictError("room_already_exists")
            return replace(
                state,
                commit_id=state.commit_id + 1,
                room_registry=state.room_registry.put_room(room),
            )

        await self._mutate(create)
        return room.room_id

    async def async_update(self, room_id: str, update: Callable[[Room], Room]) -> None:
        """Apply a typed patch under the writer lock, preserving runtime facts."""

        def mutate(state: OrchestratorState) -> OrchestratorState:
            old = state.room_registry.resolve(room_id)
            new = update(old)
            if (
                new.room_id,
                new.release,
                new.last_cleaning,
                new.last_confirmed,
                new.occupancy,
            ) != (
                old.room_id,
                old.release,
                old.last_cleaning,
                old.last_confirmed,
                old.occupancy,
            ):
                raise ValidationError("room_configuration_changes_runtime_state")
            if new == old:
                return state
            if self._is_active(state, old):
                raise ConflictError("room_has_active_job")
            if self._counter_source(new.due_policy) != self._counter_source(
                old.due_policy
            ):
                new = replace(
                    new,
                    occupancy=OccupancyCounter(
                        epoch=old.occupancy.epoch + 1, observed_at=self._clock()
                    ),
                )
            return replace(
                state,
                commit_id=state.commit_id + 1,
                room_registry=state.room_registry.put_room(new),
            )

        await self._mutate(mutate)

    async def async_remove(self, room_id: str) -> None:
        """Exclude a room while preserving historical identity and import exclusion."""
        await self.async_update(room_id, lambda room: replace(room, enabled=False))

    async def async_grant(
        self, room_id: str, kind: ReleaseKind, duration_seconds: float | None = None
    ) -> str:
        """Issue a distinct grant without altering permissions of admitted jobs."""
        if (kind is ReleaseKind.TIMED) != (duration_seconds is not None):
            raise ValidationError("release_duration_mismatch")
        if duration_seconds is not None:
            seconds(duration_seconds, positive=True)
        grant_id = self._id_factory()

        def grant(state: OrchestratorState) -> OrchestratorState:
            room = state.room_registry.resolve(room_id)
            if not room.enabled or room.area_missing:
                raise ConflictError("room_unavailable")
            now = self._clock()
            release = RoomRelease(
                grant_id,
                kind,
                now,
                None
                if duration_seconds is None
                else now + timedelta(seconds=duration_seconds),
                queue_run_id=state.queue_run.run_id
                if kind is ReleaseKind.QUEUE_RUN
                and state.queue_run
                and state.queue_run.active
                else None,
            )
            return replace(
                state,
                commit_id=state.commit_id + 1,
                room_registry=state.room_registry.put_room(
                    replace(room, release=release)
                ),
            )

        await self._mutate(grant)
        return grant_id

    async def async_revoke(self, room_id: str) -> None:
        """Revoke future admissions; cancellation remains a separate command."""

        def revoke(state: OrchestratorState) -> OrchestratorState:
            room = state.room_registry.resolve(room_id)
            if room.release is None:
                return state
            return replace(
                state,
                commit_id=state.commit_id + 1,
                room_registry=state.room_registry.put_room(replace(room, release=None)),
            )

        await self._mutate(revoke)

    async def async_record_receipt(self, receipt: CleaningReceipt) -> None:
        """Record adapter-validated success through the global transaction."""

        def record(state: OrchestratorState) -> OrchestratorState:
            if receipt.completed_at > self._clock():
                raise ValidationError("completion_in_future")
            registry = state.room_registry.record(receipt)
            return (
                state
                if registry is state.room_registry
                else replace(
                    state, commit_id=state.commit_id + 1, room_registry=registry
                )
            )

        await self._mutate(record)

    async def async_import_areas(self, areas: Mapping[str, AreaSnapshot]) -> None:
        """Reconcile a complete HA registry snapshot without re-enabling exclusions."""

        def reconcile(state: OrchestratorState) -> OrchestratorState:
            registry = state.room_registry
            rooms = dict(registry.rooms)
            existing = {
                room.area_id: room
                for room in rooms.values()
                if room.area_id is not None
            }
            for area_id, snapshot in areas.items():
                room = existing.get(area_id)
                if room is None:
                    room = Room(
                        self._id_factory(),
                        snapshot.name,
                        area_id=area_id,
                        floor_id=snapshot.floor_id,
                    )
                else:
                    room = replace(
                        room,
                        name=snapshot.name if room.follow_area_name else room.name,
                        floor_id=snapshot.floor_id,
                        area_missing=False,
                    )
                rooms[room.room_id] = room
            for area_id, room in existing.items():
                if area_id not in areas:
                    rooms[room.room_id] = replace(room, area_missing=True)
            updated = replace(registry, rooms=rooms)
            return (
                state
                if updated == registry
                else replace(
                    state, commit_id=state.commit_id + 1, room_registry=updated
                )
            )

        await self._mutate(reconcile)

    async def async_observe_occupancy(
        self, values: Mapping[str, str | None], now: datetime, *, restart: bool = False
    ) -> None:
        """Checkpoint occupancy or explicitly account for an unobserved restart gap."""

        def observe(state: OrchestratorState) -> OrchestratorState:
            rooms = dict(state.room_registry.rooms)
            for room_id, room in rooms.items():
                policy = room.due_policy
                source = policy.occupancy_entity_id
                if source is None or source not in values:
                    continue
                value = values[source]
                occupied = (
                    True
                    if value == policy.occupied_state
                    else False
                    if value == policy.unoccupied_state
                    else None
                )
                counter = room.occupancy.advance(now, occupied, gap=restart)
                rooms[room_id] = replace(room, occupancy=counter)
            registry = replace(state.room_registry, rooms=rooms)
            return (
                state
                if registry == state.room_registry
                else replace(
                    state, commit_id=state.commit_id + 1, room_registry=registry
                )
            )

        await self._mutate(observe)

    @staticmethod
    def _counter_source(policy: DuePolicy) -> tuple[object, ...]:
        return (
            policy.basis,
            policy.occupancy_entity_id,
            policy.occupancy_entity_registry_id,
            policy.occupied_state,
            policy.unoccupied_state,
        )

    @staticmethod
    def _is_active(state: OrchestratorState, room: Room) -> bool:
        aliases = {room.room_id, room.area_id}
        return any(
            job.state
            in {
                JobState.DISPATCHING,
                JobState.RUNNING,
                JobState.CANCELING,
                JobState.NEEDS_ATTENTION,
            }
            and any(target.area_id in aliases for target in job.intent.areas)
            for job in state.jobs.values()
        )
