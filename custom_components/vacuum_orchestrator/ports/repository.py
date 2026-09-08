"""Critical-state persistence port."""

from __future__ import annotations

from typing import Protocol

from ..domain.queue import OrchestratorState


class OrchestratorRepository(Protocol):
    """Persist the integration-wide snapshot with verifiable semantics."""

    async def async_load(self) -> OrchestratorState | None:
        """Load and verify the last committed snapshot."""

    async def async_commit(
        self, state: OrchestratorState, *, expected_previous_commit_id: int
    ) -> None:
        """Commit and independently verify one exact state snapshot."""
