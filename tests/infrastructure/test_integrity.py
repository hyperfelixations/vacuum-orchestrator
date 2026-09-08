"""Tests for verifiable critical-state snapshots."""

from __future__ import annotations

import pytest

from custom_components.vacuum_orchestrator.domain.errors import StorageIntegrityError
from custom_components.vacuum_orchestrator.infrastructure.integrity import (
    seal_snapshot,
    verify_snapshot,
)


def test_sealed_snapshot_round_trip() -> None:
    sealed = seal_snapshot({"commit_id": 7, "queue": ["job-1"]})

    assert verify_snapshot(sealed)["commit_id"] == 7


def test_tampering_is_detected() -> None:
    sealed = seal_snapshot({"commit_id": 7, "queue": ["job-1"]})
    sealed["payload"]["queue"].append("job-2")

    with pytest.raises(StorageIntegrityError, match="snapshot_digest_mismatch"):
        verify_snapshot(sealed)
