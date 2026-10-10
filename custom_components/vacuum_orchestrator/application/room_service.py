"""Validated room commands using the orchestrator's single commit path."""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from ..domain.completion import CleaningReceipt
from ..domain.due import DuePolicy, OccupancyCounter
from ..domain.errors import (
    ConflictError,
    OrchestratorError,
    ValidationError,
    located,
)
from ..domain.permissions import require, room_editable
from ..domain.queue import OrchestratorState
from ..domain.releases import GrantRequest, ReleaseKind, RoomRelease
from ..domain.room_registry import RoomRegistry
from ..domain.rooms import Room
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

    def room_id(self, reference: str) -> str:
        """Name the room of a room or HA area ID."""
        return self.registry.resolve(reference).room_id

    def active_room_id(self, reference: str) -> str:
        """Name the room of a room or HA area ID if it may take new jobs."""
        room = self.registry.resolve(reference)
        if not room.enabled or room.area_missing:
            raise ConflictError("room_unavailable", reference)
        return room.room_id

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
            require(room_editable(state, old))
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

    async def async_disable(self, room_id: str) -> None:
        """Exclude a room while preserving historical identity and import exclusion."""
        await self.async_update(room_id, lambda room: replace(room, enabled=False))

    async def async_enable(self, room_id: str) -> None:
        """Make a disabled room selectable again with its history intact."""
        await self.async_update(room_id, lambda room: replace(room, enabled=True))

    async def async_grant(
        self, room_id: str, kind: ReleaseKind, duration_seconds: float | None = None
    ) -> str:
        """Issue a distinct grant without altering permissions of admitted jobs."""
        try:
            (grant_id,) = await self.async_grant_many(
                (GrantRequest(room_id, kind, duration_seconds),)
            )
        except OrchestratorError as err:
            # One room is the whole input; there is no row to point at.
            raise type(err)(err.code, err.detail) from err
        return grant_id

    async def async_grant_many(
        self, requests: Sequence[GrantRequest]
    ) -> tuple[str, ...]:
        """Release every room in one commit or none; see dev doc "Freigaben"."""
        if not requests:
            raise ValidationError("no_rooms", path=("grants",))
        for index, request in enumerate(requests):
            with located("grants", index, "duration_seconds"):
                if (request.kind is ReleaseKind.TIMED) != (
                    request.duration_seconds is not None
                ):
                    raise ValidationError("release_duration_mismatch")
                if request.duration_seconds is not None:
                    seconds(request.duration_seconds, positive=True)
        grant_ids = tuple(self._id_factory() for _ in requests)

        def grant(state: OrchestratorState) -> OrchestratorState:
            now = self._clock()
            run = state.queue_run
            registry = state.room_registry
            granted: set[str] = set()
            for index, (request, grant_id) in enumerate(
                zip(requests, grant_ids, strict=True)
            ):
                with located("grants", index, "room"):
                    room = registry.resolve(request.room)
                    if room.room_id in granted:
                        raise ValidationError("duplicate_room", request.room)
                    if not room.enabled or room.area_missing:
                        raise ConflictError("room_unavailable", request.room)
                granted.add(room.room_id)
                release = RoomRelease(
                    grant_id,
                    request.kind,
                    now,
                    None
                    if request.duration_seconds is None
                    else now + timedelta(seconds=request.duration_seconds),
                    queue_run_id=run.run_id
                    if request.kind is ReleaseKind.QUEUE_RUN and run and run.active
                    else None,
                )
                registry = registry.put_room(replace(room, release=release))
            return replace(state, commit_id=state.commit_id + 1, room_registry=registry)

        await self._mutate(grant)
        return grant_ids

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

    async def async_revoke_many(self, room_ids: Sequence[str]) -> tuple[str, ...]:
        """Revoke several rooms in one commit; unknown or repeated rooms fail all."""
        if not room_ids:
            raise ValidationError("no_rooms", path=("rooms",))
        resolved: list[str] = []

        def revoke(state: OrchestratorState) -> OrchestratorState:
            resolved.clear()
            registry = state.room_registry
            for index, reference in enumerate(room_ids):
                with located("rooms", index):
                    room = registry.resolve(reference)
                    if room.room_id in resolved:
                        raise ValidationError("duplicate_room", reference)
                resolved.append(room.room_id)
                if room.release is not None:
                    registry = registry.put_room(replace(room, release=None))
            if registry is state.room_registry:
                return state
            return replace(state, commit_id=state.commit_id + 1, room_registry=registry)

        await self._mutate(revoke)
        return tuple(resolved)

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
