"""Exclusive per-robot physical command lane and ownership registry."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ..domain.errors import ConflictError, StaleCommandError
from ..ports.command_scope import command_guard


@dataclass(frozen=True, slots=True)
class RobotCommandTicket:
    """Generation-fenced authorization for one physical command."""

    source_robot_id: str
    generation: int
    attempt_id: str | None


class RobotSession:
    """Serialize all physical calls made to one source robot."""

    def __init__(
        self,
        source_robot_id: str,
        generation: int = 0,
        *,
        needs_attention: bool = False,
    ) -> None:
        self.source_robot_id = source_robot_id
        self._generation = generation
        self._owner_attempt_id: str | None = None
        self._needs_attention = needs_attention
        self._command_lane = asyncio.Lock()

    @property
    def generation(self) -> int:
        """Return the current in-memory fence generation."""
        return self._generation

    @property
    def needs_attention(self) -> bool:
        """Return whether physical commands are fail-closed."""
        return self._needs_attention

    def reserve(self, attempt_id: str) -> RobotCommandTicket:
        """Reserve the current generation for a new attempt."""
        if self._needs_attention:
            raise ConflictError("robot_needs_attention")
        if self._owner_attempt_id not in (None, attempt_id):
            raise ConflictError("robot_session_already_owned")
        self._owner_attempt_id = attempt_id
        return RobotCommandTicket(self.source_robot_id, self._generation, attempt_id)

    def fence(self, generation: int, *, needs_attention: bool) -> RobotCommandTicket:
        """Invalidate older tickets before cancel or recovery commands."""
        if generation <= self._generation:
            raise ConflictError("robot_generation_not_monotonic")
        self._generation = generation
        self._needs_attention = needs_attention
        self._owner_attempt_id = None
        return RobotCommandTicket(self.source_robot_id, generation, None)

    async def dispatch(
        self,
        ticket: RobotCommandTicket,
        command: Callable[[], Awaitable[None]],
        guard: Callable[[], None] | None = None,
    ) -> None:
        """Run a start command only while its ticket remains current."""
        async with self._command_lane:

            def validate() -> None:
                self._validate_ticket(ticket, require_attempt=True)
                if guard is not None:
                    guard()

            validate()
            token = command_guard.set(validate)
            try:
                await command()
            finally:
                command_guard.reset(token)

    async def cancel(
        self,
        ticket: RobotCommandTicket,
        command: Callable[[], Awaitable[None]],
    ) -> None:
        """Run stop after any in-flight start and reject stale cancel calls."""
        async with self._command_lane:
            self._validate_ticket(ticket, require_attempt=False)
            token = command_guard.set(
                lambda: self._validate_ticket(ticket, require_attempt=False)
            )
            try:
                await command()
            finally:
                command_guard.reset(token)

    def release(self, attempt_id: str) -> None:
        """Release attempt ownership after a correlated terminal outcome."""
        if self._owner_attempt_id not in (None, attempt_id):
            raise ConflictError("robot_session_owner_mismatch")
        self._owner_attempt_id = None

    def _validate_ticket(
        self, ticket: RobotCommandTicket, *, require_attempt: bool
    ) -> None:
        if ticket.source_robot_id != self.source_robot_id:
            raise StaleCommandError("source_robot_mismatch")
        if ticket.generation != self._generation:
            raise StaleCommandError("stale_robot_generation")
        if self._needs_attention:
            raise StaleCommandError("robot_needs_attention")
        if require_attempt and ticket.attempt_id != self._owner_attempt_id:
            raise StaleCommandError("stale_attempt_ownership")


class RobotOwnershipRegistry:
    """Enforce one integration owner for every stable physical robot."""

    def __init__(self) -> None:
        self._owners: dict[str, str] = {}

    def claim(self, source_robot_id: str, installation_id: str) -> None:
        """Claim a source robot idempotently for one installation."""
        owner = self._owners.get(source_robot_id)
        if owner is not None and owner != installation_id:
            raise ConflictError("source_robot_already_owned", owner)
        self._owners[source_robot_id] = installation_id

    def release(self, source_robot_id: str, installation_id: str) -> None:
        """Release only matching integration ownership."""
        owner = self._owners.get(source_robot_id)
        if owner is None:
            return
        if owner != installation_id:
            raise ConflictError("source_robot_owner_mismatch", owner)
        del self._owners[source_robot_id]
