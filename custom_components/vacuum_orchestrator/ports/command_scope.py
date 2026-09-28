"""Execution-local authorization checked before each physical adapter command."""

from collections.abc import Callable
from contextvars import ContextVar

from ..domain.requests import CommandOrigin

command_origin: ContextVar[CommandOrigin | None] = ContextVar(
    "voi_command_origin", default=None
)

command_guard: ContextVar[Callable[[], None] | None] = ContextVar(
    "voi_command_guard", default=None
)


def check_command_authorization() -> None:
    """Revalidate the current session after every asynchronous preparation step."""
    guard = command_guard.get()
    if guard is not None:
        guard()
