"""Manufacturer-neutral robot adapter port."""

from __future__ import annotations

from typing import Protocol

from ..domain.capabilities import RobotProfile
from ..domain.dispatching import RobotObservation
from ..domain.planning import DispatchAssignment, WorkUnit


class RobotAdapter(Protocol):
    """Translate exact work units into one existing HA vacuum integration."""

    @property
    def profile(self) -> RobotProfile:
        """Return the current immutable capability snapshot."""

    async def async_observe(self) -> RobotObservation:
        """Return one normalized point-in-time availability observation."""

    async def async_dispatch(
        self, unit: WorkUnit, assignment: DispatchAssignment
    ) -> None:
        """Send one planned work unit without changing its semantics."""

    async def async_cancel(self) -> None:
        """Apply the adapter's declared cancellation semantics."""
