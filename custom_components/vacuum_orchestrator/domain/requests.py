"""Persisted command attribution without a dependency on the HA runtime."""

from dataclasses import dataclass

from .validation import identifier


@dataclass(frozen=True, slots=True)
class CommandOrigin:
    """Opaque request identifiers carried to the physical execution boundary."""

    context_id: str
    user_id: str | None = None
    parent_id: str | None = None

    def __post_init__(self) -> None:
        for value in (self.context_id, self.user_id, self.parent_id):
            if value is not None:
                identifier(value)
