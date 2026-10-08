"""Fault-injection tests for verified critical commits."""

from copy import deepcopy
from dataclasses import replace

import pytest

from custom_components.vacuum_orchestrator.domain.errors import (
    ConflictError,
    StorageIntegrityError,
)
from custom_components.vacuum_orchestrator.domain.execution import (
    RobotRun,
    RunCorrelation,
)
from custom_components.vacuum_orchestrator.domain.queue import OrchestratorState
from custom_components.vacuum_orchestrator.domain.types import (
    CorrelationConfidence,
    CorrelationState,
)
from custom_components.vacuum_orchestrator.infrastructure.critical_repository import (
    CriticalOrchestratorRepository,
    MigratingOrchestratorRepository,
)
from custom_components.vacuum_orchestrator.infrastructure.integrity import (
    JsonObject,
    seal_snapshot,
)
from tests.domain.test_queue import NOW, _prepared
from tests.infrastructure.test_codec import legacy_payload


class Backend:
    atomic_writes = True

    def __init__(self) -> None:
        self.data: JsonObject | None = None
        self.swallow = False
        self.mutate_read = False

    async def async_load_raw(self) -> JsonObject | None:
        result = deepcopy(self.data)
        if self.mutate_read and result is not None:
            result["integrity_sha256"] = "bad"
        return result

    async def async_save_raw(self, data: JsonObject) -> None:
        if not self.swallow:
            self.data = deepcopy(data)


async def test_invalid_candidate_never_replaces_valid_snapshot() -> None:
    backend = Backend()
    repository = CriticalOrchestratorRepository(backend)
    prepared, _ = _prepared()
    await repository.async_commit(
        prepared, expected_previous_commit_id=prepared.commit_id - 1
    )
    original = deepcopy(backend.data)
    invalid = replace(prepared, assignments={}, commit_id=prepared.commit_id + 1)

    with pytest.raises(StorageIntegrityError):
        await repository.async_commit(
            invalid, expected_previous_commit_id=prepared.commit_id
        )

    assert backend.data == original
    assert await repository.async_load() == prepared


@pytest.mark.parametrize("outcome", ["cancelled", "failed", "completed"])
async def test_deleting_executed_job_preserves_reloadable_ledger(outcome: str) -> None:
    prepared, _ = _prepared()
    state = prepared.mark_command_sent("attempt", NOW)
    if outcome == "cancelled":
        state, _ = state.request_cancel("a", NOW)
        state = state.confirm_cancel("a", NOW)
    elif outcome == "failed":
        state = state.fail_job("a", "attempt", "device_error", NOW)
    else:
        run = RobotRun("run", "source", NOW, NOW, NOW, NOW, True)
        correlation = RunCorrelation(
            "attempt",
            "run",
            CorrelationState.MATCHED,
            CorrelationConfidence.STRONG,
            (),
            False,
        )
        state = state.complete_attempt("attempt", run, correlation, NOW)
    assert state.mark_dispatch_accepted("attempt", NOW) is state
    state = state.retry_job("a", "retry", NOW)
    deleted = state.delete_job("a", NOW)
    repository = CriticalOrchestratorRepository(Backend())

    await repository.async_commit(deleted, expected_previous_commit_id=state.commit_id)

    assert await repository.async_load() == deleted
    assert set(deleted.jobs) == {"retry"}
    assert deleted.jobs["retry"].retries_job_id == "a"
    assert not deleted.attempts
    assert not deleted.plans
    assert not deleted.work_unit_states
    assert not deleted.assignments
    assert not deleted.correlations
    assert not deleted.robot_runs
    assert deleted.robot_generations == state.robot_generations


async def test_commit_requires_monotonic_verified_readback() -> None:
    backend = Backend()
    repository = CriticalOrchestratorRepository(backend)
    state = OrchestratorState.empty("installation")

    await repository.async_commit(state, expected_previous_commit_id=-1)

    assert await repository.async_load() == state
    with pytest.raises(ConflictError, match="non_monotonic_commit"):
        await repository.async_commit(state, expected_previous_commit_id=0)


async def test_swallowed_or_corrupt_write_is_never_a_commit() -> None:
    backend = Backend()
    repository = CriticalOrchestratorRepository(backend)
    state = OrchestratorState.empty("installation")
    backend.swallow = True
    with pytest.raises(StorageIntegrityError, match="missing_after_save"):
        await repository.async_commit(state, expected_previous_commit_id=-1)

    backend.swallow = False
    backend.mutate_read = True
    with pytest.raises(StorageIntegrityError, match="snapshot_digest_mismatch"):
        await repository.async_commit(state, expected_previous_commit_id=-1)


def test_non_atomic_backend_is_rejected() -> None:
    backend = Backend()
    backend.atomic_writes = False
    with pytest.raises(StorageIntegrityError, match="atomic_writes_required"):
        CriticalOrchestratorRepository(backend)


async def test_legacy_snapshot_is_imported_once_and_left_unchanged() -> None:
    current_backend = Backend()
    legacy_backend = Backend()
    legacy_backend.data = seal_snapshot(legacy_payload(("vacuum", "mop")))
    original_legacy = deepcopy(legacy_backend.data)
    repository = MigratingOrchestratorRepository(
        CriticalOrchestratorRepository(current_backend),
        legacy_backend,
        "installation",
    )

    imported = await repository.async_load()

    assert imported is not None
    assert imported.installation_id == "installation"
    assert current_backend.data is not None
    assert legacy_backend.data == original_legacy

    legacy_backend.data = {"invalid": True}
    assert await repository.async_load() == imported


async def test_migrating_repository_delegates_new_commits() -> None:
    current_backend = Backend()
    repository = MigratingOrchestratorRepository(
        CriticalOrchestratorRepository(current_backend), Backend(), "installation"
    )
    state = OrchestratorState.empty("installation")

    assert await repository.async_load() is None
    await repository.async_commit(state, expected_previous_commit_id=-1)

    assert await repository.async_load() == state


def test_migrating_repository_rejects_non_atomic_legacy_backend() -> None:
    legacy_backend = Backend()
    legacy_backend.atomic_writes = False

    with pytest.raises(StorageIntegrityError, match="atomic_writes_required"):
        MigratingOrchestratorRepository(
            CriticalOrchestratorRepository(Backend()),
            legacy_backend,
            "installation",
        )
