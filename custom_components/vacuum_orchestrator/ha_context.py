"""HA context conversion at authenticated action and physical adapter boundaries."""

from collections.abc import Iterator
from contextlib import contextmanager

from homeassistant.core import Context

from .domain.requests import CommandOrigin
from .ports.command_scope import command_origin


@contextmanager
def request_context(context: Context) -> Iterator[None]:
    """Keep command attribution local to this request and any explicitly stored job."""
    token = command_origin.set(
        CommandOrigin(context.id, context.user_id, context.parent_id)
    )
    try:
        yield
    finally:
        command_origin.reset(token)


def physical_context() -> Context | None:
    """Reconstruct the stored request context for an outbound HA service call."""
    origin = command_origin.get()
    if origin is None:
        return None
    return Context(
        id=origin.context_id, user_id=origin.user_id, parent_id=origin.parent_id
    )
