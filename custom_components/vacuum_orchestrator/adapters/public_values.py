"""Public values telemetry may show; see dev doc "Strukturierte HA-Protokollierung"."""

from .home_assistant_vacuum import VACUUM_PHASES
from .roborock_faults import (
    DOCK_FAULTS,
    DOCK_NEUTRAL,
    ROBOT_FAULTS,
    ROBOT_NEUTRAL,
    SENSOR_FAULTS,
    STATUS_FAULTS,
)
from .roborock_status import STATUS_PHASES

# Observation reasons the adapters set themselves.
_REASONS = frozenset(
    {"entity_missing", "status_unusable", "cleaning_continues", "device_error"}
)

ADAPTER_VALUES = (
    frozenset(VACUUM_PHASES)
    | {"unknown", "unavailable"}
    | frozenset(STATUS_PHASES)
    | frozenset(ROBOT_FAULTS)
    | frozenset(DOCK_FAULTS)
    | ROBOT_NEUTRAL
    | DOCK_NEUTRAL
    | STATUS_FAULTS
    | frozenset(row[2] for row in SENSOR_FAULTS)
    | _REASONS
)
