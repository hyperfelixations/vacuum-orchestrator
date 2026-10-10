"""One map image per map, identified and named as core does."""

from __future__ import annotations

import struct
import zlib

from homeassistant.components.image import ImageEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import RoborockConfigEntry
from .entity import SimulatedEntity
from .robot import SimulatedRoborock

# Width and height differ so a viewer can tell whether it rotates the image.
SIZE = (320, 200)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RoborockConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add one image per map."""
    robot = entry.runtime_data
    async_add_entities(
        MapImage(hass, robot, item["name"]) for item in robot.spec["maps"]
    )


class MapImage(SimulatedEntity, ImageEntity):
    """A plain image of the map's size."""

    _attr_content_type = "image/png"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, hass: HomeAssistant, robot: SimulatedRoborock, name: str
    ) -> None:
        SimulatedEntity.__init__(self, robot)
        ImageEntity.__init__(self, hass)
        self._attr_unique_id = f"{robot.slug}_map_{name}"
        self._attr_name = name
        self._attr_image_last_updated = dt_util.utcnow()

    async def async_image(self) -> bytes:
        """A light grey PNG."""
        return _png(*SIZE)


def _png(width: int, height: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    rows = b"".join(b"\x00" + b"\xdd\xdd\xdd" * width for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )
