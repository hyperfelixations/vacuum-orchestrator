"""Device faults and the operations they affect; see dev doc "Gerätefehler"."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from .types import OperationKind


class FaultScope(StrEnum):
    """What a fault stops: the whole robot, one operation or the station."""

    GENERAL = "general"
    VACUUM = "vacuum"
    MOP = "mop"
    STATION = "station"
    STATION_VACUUM = "station_vacuum"
    STATION_MOP = "station_mop"
    NOTICE = "notice"


class FaultSource(StrEnum):
    """Which part of the device reports the fault."""

    ROBOT = "robot"
    DOCK = "dock"


_ALL = frozenset(OperationKind)
_VACUUM = frozenset({OperationKind.VACUUM, OperationKind.VACUUM_AND_MOP})
_MOP = frozenset({OperationKind.MOP, OperationKind.VACUUM_AND_MOP})
_OPERATIONS = {
    FaultScope.GENERAL: _ALL,
    FaultScope.VACUUM: _VACUUM,
    FaultScope.MOP: _MOP,
    FaultScope.STATION: _ALL,
    FaultScope.STATION_VACUUM: _VACUUM,
    FaultScope.STATION_MOP: _MOP,
    FaultScope.NOTICE: frozenset[OperationKind](),
}


@dataclass(frozen=True, slots=True)
class Fault:
    """One active device fault; `entity_id` is the entity reporting it."""

    code: str
    source: FaultSource
    scope: FaultScope
    entity_id: str | None = None

    @property
    def operations(self) -> frozenset[OperationKind]:
        """Return every operation the fault keeps from starting."""
        return _OPERATIONS[self.scope]


def blocking(
    faults: Iterable[Fault], operation: OperationKind | None
) -> tuple[Fault, ...]:
    """Return the faults that keep `operation` from starting; None means any."""
    return tuple(
        fault
        for fault in faults
        if fault.operations and (operation is None or operation in fault.operations)
    )


def interrupting(
    faults: Iterable[Fault], operation: OperationKind | None, *, finished: bool
) -> tuple[Fault, ...]:
    """Return the faults that stop a run.

    A fault first reported after the floor run ended normally did not stop it;
    only general faults still count then.
    """
    return tuple(
        fault
        for fault in blocking(faults, operation)
        if not finished or fault.scope is FaultScope.GENERAL
    )
