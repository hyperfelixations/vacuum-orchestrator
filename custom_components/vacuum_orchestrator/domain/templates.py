"""Stored job templates and durable deduplication of automatic room demand."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType

from .intents import JobIntent
from .types import CleaningMode, OperationKind
from .validation import identifier, instant


@dataclass(frozen=True, slots=True)
class JobTemplate:
    """A reusable intent and opt-in due rule with persistent demand tokens."""

    template_id: str
    name: str
    intent: JobIntent
    updated_at: datetime
    enabled: bool = True
    automatic: bool = False
    demand_tokens: Mapping[str, str] = field(default_factory=dict)
    all_rooms: bool = False

    def __post_init__(self) -> None:
        identifier(self.template_id)
        identifier(self.name)
        instant(self.updated_at)
        for room_id, token in self.demand_tokens.items():
            identifier(room_id)
            identifier(token)
        object.__setattr__(
            self, "demand_tokens", MappingProxyType(dict(self.demand_tokens))
        )


def requested_operations(mode: CleaningMode) -> frozenset[OperationKind]:
    """Compare demand coverage without conflating combined and sequential execution."""
    if mode is CleaningMode.VACUUM:
        return frozenset({OperationKind.VACUUM})
    if mode is CleaningMode.MOP:
        return frozenset({OperationKind.MOP})
    return frozenset({OperationKind.VACUUM, OperationKind.MOP})
