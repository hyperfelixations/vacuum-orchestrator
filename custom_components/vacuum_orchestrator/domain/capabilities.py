"""Typed adapter capability and robot profile contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType

from .errors import ValidationError
from .execution import ExecutionPolicy
from .requirements import StateRequirement
from .types import MopRoute, OperationKind, PassScope, SemanticLevel


class AreaAddressing(StrEnum):
    """Addressing mechanism available to a robot adapter."""

    HOME_ASSISTANT_AREA = "home_assistant_area"
    VENDOR_SEGMENT = "vendor_segment"


class CancelSemantics(StrEnum):
    """Observable effect promised by a cancel implementation."""

    STOP = "stop"
    STOP_THEN_RETURN = "stop_then_return"
    UNSUPPORTED = "unsupported"


class StartEvidence(StrEnum):
    """Evidence an adapter can expose for physical-run start correlation."""

    ACTIVITY_START_TRANSITION = "activity_start_transition"
    CAUSAL_RUN_TOKEN = "causal_run_token"


class CompletionEvidence(StrEnum):
    """Evidence an adapter can expose for physical-run completion correlation."""

    ACTIVITY_TERMINAL_TRANSITION = "activity_terminal_transition"
    CLEANING_HISTORY_TIMESTAMPS = "cleaning_history_timestamps"
    CAUSAL_RUN_TOKEN = "causal_run_token"


@dataclass(frozen=True, slots=True)
class PassCapability:
    """Supported pass count and exact execution scope."""

    maximum: int
    scope: PassScope

    def __post_init__(self) -> None:
        if self.maximum < 1:
            raise ValidationError("invalid_pass_capability")


@dataclass(frozen=True, slots=True)
class RobotCapabilities:
    """Immutable capability snapshot used to prove a plan executable."""

    revision: str
    operations: frozenset[OperationKind]
    area_addressing: AreaAddressing
    target_map: Mapping[str, str | tuple[str, ...]]
    map_context: str | None
    passes: PassCapability
    vacuum_levels: frozenset[SemanticLevel]
    water_levels: frozenset[SemanticLevel]
    cancel: CancelSemantics
    start_evidence: frozenset[StartEvidence]
    completion_evidence: frozenset[CompletionEvidence]
    mop_routes: frozenset[MopRoute] = frozenset()
    vendor_extensions: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.revision.strip():
            raise ValidationError("empty_capability_revision")
        object.__setattr__(self, "target_map", MappingProxyType(dict(self.target_map)))

    def targets_for(self, room_id: str) -> tuple[str, ...]:
        """Return every physical target bound to a canonical room."""
        value = self.target_map.get(room_id, ())
        return (value,) if isinstance(value, str) else value


@dataclass(frozen=True, slots=True)
class RobotProfile:
    """Configured robot and its current capability snapshot."""

    robot_id: str
    source_robot_id: str
    adapter: str
    capabilities: RobotCapabilities
    allowed_operations: frozenset[OperationKind] | None = None
    preference: int = 0
    minimum_battery: int | None = None
    execution_policy: ExecutionPolicy = field(default_factory=ExecutionPolicy)
    requirements: tuple[StateRequirement, ...] = ()

    def __post_init__(self) -> None:
        if not self.robot_id.strip() or not self.source_robot_id.strip():
            raise ValidationError("invalid_robot_identity")
        if not self.adapter.strip():
            raise ValidationError("invalid_adapter")
        if self.allowed_operations is not None and not self.allowed_operations:
            raise ValidationError("empty_allowed_operations")
        if not -100 <= self.preference <= 100:
            raise ValidationError("invalid_robot_preference")
        if self.minimum_battery is not None and not 0 <= self.minimum_battery <= 100:
            raise ValidationError("invalid_minimum_battery")

    @property
    def effective_operations(self) -> frozenset[OperationKind]:
        """Return operations allowed by both capability and user policy."""
        if self.allowed_operations is None:
            return self.capabilities.operations
        return self.capabilities.operations & self.allowed_operations
