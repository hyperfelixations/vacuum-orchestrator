"""Verified critical-state repository independent of Home Assistant I/O."""

from __future__ import annotations

from typing import Protocol

from ..domain.errors import ConflictError, StorageIntegrityError
from ..domain.queue import OrchestratorState
from .codec import (
    decode_orchestrator_state,
    encode_orchestrator_state,
    migrate_schema_one,
    migrate_schema_two,
)
from .integrity import JsonObject, seal_snapshot, verify_snapshot


class SnapshotBackend(Protocol):
    """Atomic storage backend that supports an independent read-back."""

    @property
    def atomic_writes(self) -> bool:
        """Return whether replacement writes are atomic."""

    async def async_load_raw(self) -> JsonObject | None:
        """Read the on-disk snapshot directly."""

    async def async_save_raw(self, data: JsonObject) -> None:
        """Request an atomic snapshot replacement."""


class CriticalOrchestratorRepository:
    """Persist only commits whose exact bytes can be read back and verified."""

    def __init__(self, backend: SnapshotBackend) -> None:
        if not backend.atomic_writes:
            raise StorageIntegrityError("atomic_writes_required")
        self._backend = backend

    async def async_load(self) -> OrchestratorState | None:
        """Load and validate the integration-wide verified snapshot."""
        raw = await self._backend.async_load_raw()
        if raw is None:
            return None
        return decode_orchestrator_state(verify_snapshot(raw))

    async def async_commit(
        self, state: OrchestratorState, *, expected_previous_commit_id: int
    ) -> None:
        """Write, read back, and verify an exact monotonic commit."""
        if state.commit_id != expected_previous_commit_id + 1:
            raise ConflictError("non_monotonic_commit")
        payload = encode_orchestrator_state(state)
        if decode_orchestrator_state(payload) != state:
            raise StorageIntegrityError("critical_commit_semantic_mismatch")
        sealed = seal_snapshot(payload)
        await self._backend.async_save_raw(sealed)
        read_back = await self._backend.async_load_raw()
        if read_back is None:
            raise StorageIntegrityError("critical_commit_missing_after_save")
        verified = verify_snapshot(read_back)
        if verified != verify_snapshot(sealed):
            raise StorageIntegrityError("critical_commit_readback_mismatch")
        persisted = decode_orchestrator_state(verified)
        if persisted != state:
            raise StorageIntegrityError("critical_commit_semantic_mismatch")


class MigratingOrchestratorRepository:
    """Load the current store or non-destructively import the legacy store once."""

    def __init__(
        self,
        current: CriticalOrchestratorRepository,
        legacy_backend: SnapshotBackend,
        installation_id: str,
        *,
        previous_backend: SnapshotBackend | None = None,
    ) -> None:
        if not legacy_backend.atomic_writes:
            raise StorageIntegrityError("atomic_writes_required")
        self._current = current
        self._legacy_backend = legacy_backend
        self._installation_id = installation_id
        self._previous_backend = previous_backend

    async def async_load(self) -> OrchestratorState | None:
        """Prefer current storage and import V2 before considering legacy V1."""
        current = await self._current.async_load()
        if current is not None:
            return current
        if self._previous_backend is not None:
            previous = await self._previous_backend.async_load_raw()
            if previous is not None:
                migrated = migrate_schema_two(verify_snapshot(previous))
                if migrated.installation_id != self._installation_id:
                    raise StorageIntegrityError(
                        "installation_storage_ownership_mismatch"
                    )
                await self._current.async_commit(
                    migrated, expected_previous_commit_id=migrated.commit_id - 1
                )
                return migrated
        legacy_raw = await self._legacy_backend.async_load_raw()
        if legacy_raw is None:
            return None
        migrated = migrate_schema_one(
            verify_snapshot(legacy_raw), self._installation_id
        )
        await self._current.async_commit(
            migrated, expected_previous_commit_id=migrated.commit_id - 1
        )
        confirmed = await self._current.async_load()
        if confirmed != migrated:
            raise StorageIntegrityError("legacy_import_verification_failed")
        return confirmed

    async def async_commit(
        self, state: OrchestratorState, *, expected_previous_commit_id: int
    ) -> None:
        """Delegate all new commits to the current verified store."""
        await self._current.async_commit(
            state, expected_previous_commit_id=expected_previous_commit_id
        )
