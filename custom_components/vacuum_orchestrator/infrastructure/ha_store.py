"""Home Assistant Store backend with direct on-disk commit verification."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from ..const import STORE_MINOR_VERSION, STORE_VERSION
from ..domain.errors import StorageIntegrityError
from .integrity import JsonObject


class HomeAssistantSnapshotBackend:
    """Use HA atomic writes while independently reading the storage file back."""

    atomic_writes = True

    def __init__(
        self,
        hass: HomeAssistant,
        key: str,
        *,
        store_version: int = STORE_VERSION,
        store_minor_version: int = STORE_MINOR_VERSION,
    ) -> None:
        self._hass = hass
        self._store_version = store_version
        self._store_minor_version = store_minor_version
        self._store: Store[JsonObject] = Store(
            hass,
            store_version,
            key,
            minor_version=store_minor_version,
            atomic_writes=True,
        )
        self._path = Path(self._store.path)

    async def async_load_raw(self) -> JsonObject | None:
        """Read and validate the HA wrapper directly, bypassing Store recovery."""
        return await self._hass.async_add_executor_job(self._read_direct)

    async def async_save_raw(self, data: JsonObject) -> None:
        """Request HA's atomic replacement; callers must verify via read-back."""
        await self._store.async_save(data)

    def _read_direct(self) -> JsonObject | None:
        if not self._path.exists():
            return None
        try:
            wrapper = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as err:
            raise StorageIntegrityError("critical_storage_unreadable") from err
        if not isinstance(wrapper, dict):
            raise StorageIntegrityError("invalid_home_assistant_store_wrapper")
        if (
            wrapper.get("version") != self._store_version
            or wrapper.get("minor_version", 1) != self._store_minor_version
            or wrapper.get("key") != self._store.key
        ):
            raise StorageIntegrityError("unsupported_home_assistant_store_wrapper")
        data: Any = wrapper.get("data")
        if not isinstance(data, dict) or not all(isinstance(key, str) for key in data):
            raise StorageIntegrityError("invalid_home_assistant_store_data")
        return cast(JsonObject, data)
