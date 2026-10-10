"""Room aggregate transitions committed with the global execution ledger."""

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from types import MappingProxyType

from .completion import CleaningReceipt, CompletionQuality
from .errors import ConflictError, ValidationError
from .rooms import Room, validate_room_registry
from .validation import identifier


@dataclass(frozen=True, slots=True)
class RoomAdmission:
    """Job-scoped permission survives grant expiry, revocation and renewal."""

    room_id: str
    grant_id: str
    started: bool = False

    def __post_init__(self) -> None:
        identifier(self.room_id)
        identifier(self.grant_id)


@dataclass(frozen=True, slots=True)
class RoomRegistry:
    """Canonical room configuration, completion facts and admitted job scopes."""

    rooms: Mapping[str, Room] = field(default_factory=dict)
    receipts: Mapping[str, CleaningReceipt] = field(default_factory=dict)
    admissions: Mapping[str, tuple[RoomAdmission, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in ("rooms", "receipts", "admissions"):
            object.__setattr__(
                self, field_name, MappingProxyType(dict(getattr(self, field_name)))
            )
        validate_room_registry(self.rooms)
        for key, receipt in self.receipts.items():
            if (
                key != receipt.receipt_id
                or not set(receipt.room_ids) <= self.rooms.keys()
            ):
                raise ValidationError("receipt_registry_mismatch")
        for admissions in self.admissions.values():
            ids = [admission.room_id for admission in admissions]
            if (
                not ids
                or len(ids) != len(set(ids))
                or not set(ids) <= self.rooms.keys()
            ):
                raise ValidationError("invalid_room_admission")
        for room in self.rooms.values():
            for operation, stamp in (
                *room.last_cleaning.items(),
                *room.last_confirmed.items(),
            ):
                fact = self.receipts.get(stamp.receipt_id)
                if (
                    fact is None
                    or room.room_id not in fact.room_ids
                    or operation not in fact.operations
                    or stamp.completed_at != fact.completed_at
                    or stamp.quality != fact.quality
                ):
                    raise ValidationError("cleaning_projection_mismatch")

    def put_room(self, room: Room) -> RoomRegistry:
        """Validate replacement against all canonical and physical identities."""
        return replace(self, rooms={**self.rooms, room.room_id: room})

    def active_room_ids(self) -> tuple[str, ...]:
        """Return enabled rooms whose area still exists, whatever robots reach."""
        return tuple(
            room.room_id
            for room in self.rooms.values()
            if room.enabled and not room.area_missing
        )

    def resolve(self, reference: str) -> Room:
        """Accept a stable VOI identifier or an unambiguous legacy HA area alias."""
        room = self.rooms.get(reference)
        if room is None:
            room = next(
                (item for item in self.rooms.values() if item.area_id == reference),
                None,
            )
        if room is None:
            raise ValidationError("unknown_room", reference)
        return room

    def admit(
        self, job_id: str, room_ids: tuple[str, ...], now: datetime
    ) -> RoomRegistry:
        """Reserve every target grant atomically before any physical start."""
        room_ids = tuple(self.resolve(reference).room_id for reference in room_ids)
        if job_id in self.admissions:
            if set(room_ids) != {item.room_id for item in self.admissions[job_id]}:
                raise ConflictError("admission_scope_changed")
            return self
        rooms = dict(self.rooms)
        admissions = []
        for room_id in room_ids:
            room = self.resolve(room_id)
            if not room.enabled or room.area_missing or room.release is None:
                raise ConflictError("room_not_released", room_id)
            grant = room.release.reserve(job_id, now)
            rooms[room.room_id] = replace(room, release=grant)
            admissions.append(RoomAdmission(room.room_id, grant.grant_id))
        return replace(
            self, rooms=rooms, admissions={**self.admissions, job_id: tuple(admissions)}
        )

    def allows_job(self, job_id: str, room_id: str, now: datetime) -> bool:
        """Recheck unstarted admissions while preserving a started job's scope."""
        room = self.resolve(room_id)
        admission = next(
            (
                item
                for item in self.admissions.get(job_id, ())
                if item.room_id == room.room_id
            ),
            None,
        )
        if admission is not None and admission.started:
            return True
        grant = room.release
        if not room.enabled or room.area_missing or grant is None:
            return False
        if admission is not None and admission.grant_id != grant.grant_id:
            return False
        return grant.allows_new_job(now) or (
            grant.reserved_job_id == job_id
            and not grant.consumed
            and now >= grant.granted_at
        )

    def mark_started(self, job_id: str) -> RoomRegistry:
        """Consume only original grant generations, preserving later grants."""
        if job_id not in self.admissions:
            return self
        rooms = dict(self.rooms)
        admissions = []
        for admission in self.admissions[job_id]:
            room = rooms[admission.room_id]
            if room.release is not None and room.release.grant_id == admission.grant_id:
                rooms[room.room_id] = replace(
                    room, release=room.release.consume(job_id)
                )
            admissions.append(replace(admission, started=True))
        return replace(
            self, rooms=rooms, admissions={**self.admissions, job_id: tuple(admissions)}
        )

    def finish(self, job_id: str, *, never_started: bool = False) -> RoomRegistry:
        """Release execution admission after terminal, physically resolved work."""
        if job_id not in self.admissions:
            return self
        rooms = dict(self.rooms)
        for admission in self.admissions[job_id]:
            room = rooms[admission.room_id]
            if room.release is not None and room.release.grant_id == admission.grant_id:
                release = room.release
                release = (
                    release.unreserve(job_id)
                    if never_started and not admission.started
                    else release.consume(job_id)
                )
                rooms[room.room_id] = replace(room, release=release)
        return replace(
            self,
            rooms=rooms,
            admissions={
                key: value for key, value in self.admissions.items() if key != job_id
            },
        )

    def withdraw(self, receipt_ids: frozenset[str]) -> RoomRegistry:
        """Remove receipts; rooms fall back to the remaining ones."""
        withdrawn = frozenset(receipt_ids & self.receipts.keys())
        if not withdrawn:
            return self
        receipts = {
            key: value for key, value in self.receipts.items() if key not in withdrawn
        }
        rooms = {
            room_id: room.without_receipts(withdrawn, receipts.values())
            for room_id, room in self.rooms.items()
        }
        return replace(self, rooms=rooms, receipts=receipts)

    def record(self, receipt: CleaningReceipt) -> RoomRegistry:
        """Apply a unique successful fact, allowing only an evidence upgrade."""
        previous = self.receipts.get(receipt.receipt_id)
        if previous == receipt:
            return self
        if previous is not None and (
            previous.quality is not CompletionQuality.DERIVED
            or receipt.quality is not CompletionQuality.CONFIRMED
            or replace(previous, quality=receipt.quality, evidence=receipt.evidence)
            != receipt
        ):
            raise ConflictError("receipt_identity_conflict")
        rooms = dict(self.rooms)
        for room_id in receipt.room_ids:
            room = self.resolve(room_id)
            rooms[room.room_id] = room.apply_receipt(receipt)
        return replace(
            self, rooms=rooms, receipts={**self.receipts, receipt.receipt_id: receipt}
        )
