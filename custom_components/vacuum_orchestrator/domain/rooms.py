"""Canonical rooms, robot-specific targeting, and cleaning projections."""

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from types import MappingProxyType

from .completion import CleaningReceipt, CompletionQuality
from .due import CleaningStamp, DuePolicy, DueReport, OccupancyCounter, evaluate_due
from .errors import ValidationError
from .releases import RoomRelease
from .requirements import StateRequirement
from .types import OperationKind
from .validation import identifier


@dataclass(frozen=True, slots=True)
class RoomBinding:
    """One robot's map-scoped representation of a canonical room."""

    robot_id: str
    target_ids: tuple[str, ...]
    map_id: str | None = None

    def __post_init__(self) -> None:
        identifier(self.robot_id)
        if not self.target_ids or len(set(self.target_ids)) != len(self.target_ids):
            raise ValidationError("invalid_room_targets")
        for target in self.target_ids:
            identifier(target)
        if self.map_id is not None:
            identifier(self.map_id)


@dataclass(frozen=True, slots=True)
class Room:
    """Persistent room independent of HA areas and entity exposure."""

    room_id: str
    name: str
    area_id: str | None = None
    floor_id: str | None = None
    enabled: bool = True
    area_missing: bool = False
    follow_area_name: bool = True
    bindings: tuple[RoomBinding, ...] = ()
    requirements: tuple[StateRequirement, ...] = ()
    release: RoomRelease | None = None
    due_policy: DuePolicy = field(default_factory=DuePolicy)
    occupancy: OccupancyCounter = field(default_factory=OccupancyCounter)
    last_cleaning: Mapping[OperationKind, CleaningStamp] = field(default_factory=dict)
    last_confirmed: Mapping[OperationKind, CleaningStamp] = field(default_factory=dict)

    def __post_init__(self) -> None:
        identifier(self.room_id)
        identifier(self.name, "invalid_room_name")
        for reference in (self.area_id, self.floor_id):
            if reference is not None:
                identifier(reference)
        keys = {(binding.robot_id, binding.map_id) for binding in self.bindings}
        if len(keys) != len(self.bindings):
            raise ValidationError("duplicate_room_binding")
        for field_name in ("last_cleaning", "last_confirmed"):
            values = dict(getattr(self, field_name))
            if set(values) - {OperationKind.VACUUM, OperationKind.MOP}:
                raise ValidationError("invalid_cleaning_projection")
            object.__setattr__(self, field_name, MappingProxyType(values))
        for operation, confirmed in self.last_confirmed.items():
            effective = self.last_cleaning.get(operation)
            if (
                confirmed.quality is not CompletionQuality.CONFIRMED
                or effective is None
                or confirmed.completed_at > effective.completed_at
            ):
                raise ValidationError("invalid_confirmed_projection")

    def apply_receipt(self, receipt: CleaningReceipt) -> Room:
        """Advance operation timestamps monotonically while retaining proof quality."""
        if self.room_id not in receipt.room_ids:
            raise ValidationError("receipt_room_mismatch")
        occupancy = self.occupancy
        known = (
            occupancy.observed_at is not None
            and receipt.completed_at >= occupancy.observed_at
        )
        if known:
            occupancy = occupancy.advance(receipt.completed_at, occupancy.occupied)
        stamp = CleaningStamp(
            receipt.receipt_id,
            receipt.completed_at,
            receipt.quality,
            occupancy.epoch,
            occupancy.occupied_seconds,
            occupancy.unknown_seconds,
            known,
        )
        previous_stamp = next(
            (
                value
                for value in self.last_cleaning.values()
                if value.receipt_id == receipt.receipt_id
            ),
            None,
        )
        if previous_stamp is not None:
            stamp = replace(previous_stamp, quality=receipt.quality)
        effective = dict(self.last_cleaning)
        confirmed = dict(self.last_confirmed)
        for operation in receipt.operations:
            previous = effective.get(operation)
            if (
                previous is None
                or stamp.completed_at > previous.completed_at
                or (
                    stamp.completed_at == previous.completed_at
                    and stamp.quality is CompletionQuality.CONFIRMED
                )
            ):
                effective[operation] = stamp
            if stamp.quality is CompletionQuality.CONFIRMED:
                previous = confirmed.get(operation)
                if previous is None or stamp.completed_at >= previous.completed_at:
                    confirmed[operation] = stamp
        return replace(
            self, occupancy=occupancy, last_cleaning=effective, last_confirmed=confirmed
        )

    def due(self, operation: OperationKind, now: datetime) -> DueReport:
        """Project due state without mutating counters or persisting clock ticks."""
        if operation not in {OperationKind.VACUUM, OperationKind.MOP}:
            raise ValidationError("due_requires_single_operation")
        interval = (
            self.due_policy.vacuum_seconds
            if operation is OperationKind.VACUUM
            else self.due_policy.mop_seconds
        )
        return evaluate_due(
            self.due_policy.basis,
            interval,
            self.last_cleaning.get(operation),
            self.occupancy,
            now,
        )


def validate_room_registry(rooms: Mapping[str, Room]) -> None:
    """Reject identity aliases and conflicting physical target assignments."""
    area_ids: set[str] = set()
    physical: set[tuple[str, str | None, str]] = set()
    for room_id, room in rooms.items():
        if room.room_id != room_id:
            raise ValidationError("room_identity_mismatch")
        if room.area_id is not None:
            if room.area_id in rooms and room.area_id != room_id:
                raise ValidationError("ambiguous_room_alias")
            if room.area_id in area_ids:
                raise ValidationError("duplicate_area_mapping")
            area_ids.add(room.area_id)
        for binding in room.bindings:
            targets = {
                (binding.robot_id, binding.map_id, target)
                for target in binding.target_ids
            }
            if targets & physical:
                raise ValidationError("overlapping_room_mapping")
            physical.update(targets)
