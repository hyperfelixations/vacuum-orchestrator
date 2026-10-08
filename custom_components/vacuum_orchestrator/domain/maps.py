"""The maps a robot knows, for display; dispatch never reads these."""

from dataclasses import dataclass
from enum import StrEnum


class MapsUnavailable(StrEnum):
    """Why a robot offers no map inventory."""

    NOT_SUPPORTED = "inventory_not_supported"
    UNAVAILABLE = "inventory_unavailable"


@dataclass(frozen=True, slots=True)
class MapSegment:
    """One vendor segment of a map with the name the vendor gives it."""

    segment_id: str
    name: str | None


@dataclass(frozen=True, slots=True)
class RobotMap:
    """One stored map; `image_ref` names the entity that renders it."""

    map_id: str
    name: str
    current: bool
    image_ref: str | None
    segments: tuple[MapSegment, ...]


@dataclass(frozen=True, slots=True)
class RobotMaps:
    """A robot's maps and the image of the map it uses now."""

    maps: tuple[RobotMap, ...] = ()
    current_image_ref: str | None = None
    unavailable_reason: MapsUnavailable | None = None
