"""Whether a robot can clean a room, and why not."""

from collections.abc import Iterable
from dataclasses import dataclass, replace
from enum import StrEnum


class ReachStatus(StrEnum):
    """One reason per room; only `reachable` yields cleaning targets."""

    REACHABLE = "reachable"
    NO_AREA = "no_area"
    AREA_EXCLUDED = "area_excluded"
    AREA_NOT_MAPPED = "area_not_mapped"
    MAP_UNKNOWN = "map_unknown"
    NOT_ON_CURRENT_MAP = "not_on_current_map"
    BINDING_INVALID = "binding_invalid"
    OVERLAP = "overlap"


@dataclass(frozen=True, slots=True)
class RoomReach:
    """Targets a robot sends for a room and targets it leaves out."""

    room_id: str
    status: ReachStatus
    targets: tuple[str, ...] = ()
    ignored: tuple[str, ...] = ()
    # Physical units behind the targets, compared across rooms for overlaps.
    physical: tuple[str, ...] = ()


def without_overlaps(reach: Iterable[RoomReach]) -> tuple[RoomReach, ...]:
    """Mark rooms sharing a physical unit; neither may own it alone."""
    items = tuple(reach)
    owners: dict[str, set[str]] = {}
    for item in items:
        if item.status is ReachStatus.REACHABLE:
            for unit in item.physical:
                owners.setdefault(unit, set()).add(item.room_id)
    return tuple(
        replace(item, status=ReachStatus.OVERLAP, targets=())
        if item.status is ReachStatus.REACHABLE
        and any(len(owners[unit]) > 1 for unit in item.physical)
        else item
        for item in items
    )


def reachable_targets(reach: Iterable[RoomReach]) -> dict[str, tuple[str, ...]]:
    """Return the executable target map of the reachable rooms."""
    return {
        item.room_id: item.targets
        for item in reach
        if item.status is ReachStatus.REACHABLE
    }
