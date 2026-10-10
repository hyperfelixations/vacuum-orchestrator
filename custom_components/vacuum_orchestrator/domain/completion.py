"""Cleaning receipts independent of execution-ledger retention."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from .errors import ValidationError
from .types import OperationKind
from .validation import identifier, instant


class CompletionQuality(StrEnum):
    """Quality of successful completion, independent of intended mode."""

    CONFIRMED = "confirmed"
    DERIVED = "derived"


# Evidence codes a person should see with a completion; see dev doc
# "Abweichungen". All other evidence codes describe the proof itself.
NOTES = ("mode_changed", "scope_changed", "end_not_observed")

_COVERED = {
    OperationKind.VACUUM: frozenset({OperationKind.VACUUM}),
    OperationKind.MOP: frozenset({OperationKind.MOP}),
    OperationKind.VACUUM_AND_MOP: frozenset({OperationKind.VACUUM, OperationKind.MOP}),
}


def evidenced_operation(
    planned: OperationKind, observed: tuple[OperationKind, ...]
) -> OperationKind | None:
    """Return what every observed mode covered of the planned operation.

    Without mode evidence the planned operation stands. Vacuuming and mopping
    together covers either alone.
    """
    covered = set(_COVERED[planned])
    for operation in observed:
        covered &= _COVERED[operation]
    return next(
        (operation for operation, parts in _COVERED.items() if parts == covered),
        None,
    )


@dataclass(frozen=True, slots=True)
class JobCompletion:
    """How a completed job was proven and what a person should know about it."""

    quality: CompletionQuality
    notes: tuple[str, ...]


class CleaningSource(StrEnum):
    """Origin of a physical cleaning run."""

    VOI = "voi"
    EXTERNAL = "external"


@dataclass(frozen=True, slots=True)
class CleaningReceipt:
    """Idempotent successful cleaning fact with explicit scope and provenance."""

    receipt_id: str
    source: CleaningSource
    source_id: str
    room_ids: tuple[str, ...]
    operation: OperationKind
    completed_at: datetime
    quality: CompletionQuality
    evidence: tuple[str, ...]

    def __post_init__(self) -> None:
        identifier(self.receipt_id)
        identifier(self.source_id)
        instant(self.completed_at)
        if not self.room_ids or len(set(self.room_ids)) != len(self.room_ids):
            raise ValidationError("invalid_receipt_rooms")
        for room_id in self.room_ids:
            identifier(room_id)
        if not self.evidence:
            raise ValidationError("receipt_evidence_required")
        for reason in self.evidence:
            identifier(reason)
        if (
            self.source is CleaningSource.EXTERNAL
            and self.quality is not CompletionQuality.CONFIRMED
        ):
            raise ValidationError("external_receipt_requires_confirmation")

    @property
    def operations(self) -> tuple[OperationKind, ...]:
        """Return the independent room timestamps justified by this receipt."""
        if self.operation is OperationKind.VACUUM_AND_MOP:
            return (OperationKind.VACUUM, OperationKind.MOP)
        return (self.operation,)
