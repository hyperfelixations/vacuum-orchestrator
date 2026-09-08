"""Home Assistant storage-backend contract tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant

from custom_components.vacuum_orchestrator.domain.errors import StorageIntegrityError
from custom_components.vacuum_orchestrator.infrastructure.ha_store import (
    HomeAssistantSnapshotBackend,
)


async def test_backend_sets_atomic_store_and_reads_actual_disk(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = HomeAssistantSnapshotBackend(hass, "vacuum_orchestrator.test")

    async def write_actual_file(data: dict[str, object]) -> None:
        await hass.async_add_executor_job(backend._store._write_data, data)

    monkeypatch.setattr(backend._store, "_async_write_data", write_actual_file)

    await backend.async_save_raw({"payload": {"commit_id": 1}})

    assert backend.atomic_writes is True
    assert await backend.async_load_raw() == {"payload": {"commit_id": 1}}


async def test_malformed_store_is_not_renamed_or_treated_as_empty(
    hass: HomeAssistant,
) -> None:
    backend = HomeAssistantSnapshotBackend(hass, "vacuum_orchestrator.corrupt")
    path = Path(hass.config.path(".storage", "vacuum_orchestrator.corrupt"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(StorageIntegrityError, match="critical_storage_unreadable"):
        await backend.async_load_raw()

    assert path.read_text(encoding="utf-8") == "{not-json"


async def test_wrong_store_wrapper_fails_closed(
    hass: HomeAssistant,
) -> None:
    backend = HomeAssistantSnapshotBackend(hass, "vacuum_orchestrator.wrong")
    path = Path(hass.config.path(".storage", "vacuum_orchestrator.wrong"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 99,
                "minor_version": 1,
                "key": "vacuum_orchestrator.wrong",
                "data": {},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        StorageIntegrityError, match="unsupported_home_assistant_store_wrapper"
    ):
        await backend.async_load_raw()


async def test_missing_and_structurally_invalid_store_are_distinct(
    hass: HomeAssistant,
) -> None:
    missing = HomeAssistantSnapshotBackend(hass, "vacuum_orchestrator.missing")
    assert await missing.async_load_raw() is None

    path = Path(hass.config.path(".storage", "vacuum_orchestrator.invalid"))
    path.parent.mkdir(parents=True, exist_ok=True)
    invalid = HomeAssistantSnapshotBackend(hass, "vacuum_orchestrator.invalid")
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(
        StorageIntegrityError, match="invalid_home_assistant_store_wrapper"
    ):
        await invalid.async_load_raw()

    path.write_text(
        json.dumps(
            {
                "version": 2,
                "minor_version": 0,
                "key": "vacuum_orchestrator.invalid",
                "data": [],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(
        StorageIntegrityError, match="invalid_home_assistant_store_data"
    ):
        await invalid.async_load_raw()
