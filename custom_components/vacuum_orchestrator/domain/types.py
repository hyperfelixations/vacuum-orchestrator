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


class VacuumLevel(StrEnum):
    """Manufacturer-neutral suction level; see dev doc "Stufen"."""

    OFF = "off"
    LOW = "low"
    STANDARD = "standard"
    HIGH = "high"
    MAXIMUM = "maximum"
    MAXIMUM_PLUS = "maximum_plus"


class WaterLevel(StrEnum):
    """Manufacturer-neutral mop water level."""

    OFF = "off"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class MopRoute(StrEnum):
    """Manufacturer-neutral mop route."""

    FAST = "fast"
    STANDARD = "standard"
    DEEP = "deep"
    DEEP_PLUS = "deep_plus"


SettingValue = VacuumLevel | WaterLevel | MopRoute

# Ordered ladders of selectable values; `off` stays internal to adapters.
VACUUM_LADDER: tuple[VacuumLevel, ...] = (
    VacuumLevel.LOW,
    VacuumLevel.STANDARD,
    VacuumLevel.HIGH,
    VacuumLevel.MAXIMUM,
    VacuumLevel.MAXIMUM_PLUS,
)
WATER_LADDER: tuple[WaterLevel, ...] = (
    WaterLevel.LOW,
    WaterLevel.MEDIUM,
    WaterLevel.HIGH,
)
ROUTE_LADDER: tuple[MopRoute, ...] = (
    MopRoute.FAST,
    MopRoute.STANDARD,
    MopRoute.DEEP,
    MopRoute.DEEP_PLUS,
)


def nearest_supported[T: SettingValue](
    requested: T, ladder: tuple[T, ...], supported: frozenset[T]
) -> T | None:
    """Pick the supported rung closest to a request; ties prefer the lower one."""
    candidates = [value for value in ladder if value in supported]
    if not candidates:
        return None
    position = ladder.index(requested)
    return min(
        candidates,
        key=lambda value: (abs(ladder.index(value) - position), ladder.index(value)),
    )


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
