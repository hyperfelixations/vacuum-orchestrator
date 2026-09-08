"""Canonical manufacturer-neutral domain enumerations."""

from enum import StrEnum

from .errors import ValidationError


class CleaningMode(StrEnum):
    """User-facing cleaning intent with four distinct semantics."""

    VACUUM = "vacuum"
    MOP = "mop"
    VACUUM_AND_MOP = "vacuum_and_mop"
    VACUUM_THEN_MOP = "vacuum_then_mop"


_MODE_ALIASES: dict[str, CleaningMode] = {
    "vacuum": CleaningMode.VACUUM,
    "vac": CleaningMode.VACUUM,
    "mop": CleaningMode.MOP,
    "vacuum_and_mop": CleaningMode.VACUUM_AND_MOP,
    "vac_and_mop": CleaningMode.VACUUM_AND_MOP,
    "vacuum_then_mop": CleaningMode.VACUUM_THEN_MOP,
    "vac_then_mop": CleaningMode.VACUUM_THEN_MOP,
}


def parse_cleaning_mode(value: object) -> CleaningMode:
    """Normalize the seven supported public keys to four canonical modes."""
    if isinstance(value, CleaningMode):
        return value
    if not isinstance(value, str):
        raise ValidationError("invalid_cleaning_mode")
    try:
        return _MODE_ALIASES[value.strip().lower()]
    except KeyError as err:
        raise ValidationError("invalid_cleaning_mode", value) from err


class OperationKind(StrEnum):
    """Atomic cleaning operation."""

    VACUUM = "vacuum"
    MOP = "mop"
    VACUUM_AND_MOP = "vacuum_and_mop"


class SemanticLevel(StrEnum):
    """Manufacturer-neutral intensity request."""

    OFF = "off"
    LOW = "low"
    STANDARD = "standard"
    MEDIUM = "medium"
    HIGH = "high"
    MAXIMUM = "maximum"
    AUTO = "auto"


class MopRoute(StrEnum):
    """Manufacturer-neutral mop route request."""

    STANDARD = "standard"
    DEEP = "deep"
    FAST = "fast"
    AUTO = "auto"


class SettingsPolicy(StrEnum):
    """Handling of unsupported non-essential cleaning preferences."""

    BEST_EFFORT = "best_effort"
    STRICT = "strict"


class PassScope(StrEnum):
    """Semantic scope of a repeated cleaning operation."""

    TARGET_SET = "target_set"
    PER_TARGET = "per_target"


class JobState(StrEnum):
    """Persisted job lifecycle state."""

    QUEUED = "queued"
    DISPATCHING = "dispatching"
    RUNNING = "running"
    CANCELING = "canceling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    NEEDS_ATTENTION = "needs_attention"


class WorkUnitState(StrEnum):
    """Persisted progress of one immutable work unit."""

    PENDING = "pending"
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    NEEDS_ATTENTION = "needs_attention"


class QueueMode(StrEnum):
    """Persistent queue control mode."""

    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"


class MoveDirection(StrEnum):
    """Simple user-facing queue movement."""

    UP = "up"
    DOWN = "down"
    TOP = "top"
    BOTTOM = "bottom"


class ReadinessState(StrEnum):
    """Derived readiness of a job independent of robot availability."""

    READY = "ready"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


class RobotAvailabilityState(StrEnum):
    """Normalized ability of a robot to accept a new work unit now."""

    AVAILABLE = "available"
    BUSY = "busy"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class AttemptState(StrEnum):
    """Execution-attempt lifecycle state."""

    PREPARED = "prepared"
    COMMAND_SENT = "command_sent"
    START_CONFIRMED = "start_confirmed"
    COMPLETION_PENDING = "completion_pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCEL_PENDING = "cancel_pending"
    CANCELLED = "cancelled"
    RECOVERY_REQUIRED = "recovery_required"


class CorrelationState(StrEnum):
    """Relationship between an attempt and an observed physical run."""

    MATCHED = "matched"
    UNCERTAIN = "uncertain"
    EXTERNAL_OR_CONFLICTING = "external_or_conflicting"


class CorrelationConfidence(StrEnum):
    """Strength of the available correlation evidence."""

    NONE = "none"
    WEAK = "weak"
    STRONG = "strong"
    CONFLICTING = "conflicting"
