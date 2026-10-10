"""HA-compatible diagnostic logging with bounded repetition and private IDs."""

import hashlib
import hmac
import json
import logging
import secrets
from collections import OrderedDict
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from time import monotonic

from ..domain.completion import CompletionQuality
from ..domain.faults import FaultScope, FaultSource
from ..domain.intents import SETTING_NAMES
from ..domain.monitoring import MonitorAction
from ..domain.types import (
    AttemptState,
    CleaningMode,
    JobState,
    OperationKind,
    QueueMode,
    ReadinessState,
    RecoveryResolution,
    RobotAvailabilityState,
    RobotPhase,
    WorkUnitState,
)
from ..ports.telemetry import Scalar, TelemetryEvent

_LOGGER = logging.getLogger(__name__)
# Schema of sanitized records and diagnostic exports.
DIAGNOSTIC_VERSION = 2
# Own error codes: the `exceptions` keys, kept equal to the raised codes by
# tests/test_error_translations.py.
ERROR_CODES = frozenset(
    json.loads(
        (Path(__file__).parents[1] / "strings.json").read_text(encoding="utf-8")
    )["exceptions"]
)
_ENUMS: tuple[type[StrEnum], ...] = (
    AttemptState,
    CleaningMode,
    CompletionQuality,
    FaultScope,
    FaultSource,
    JobState,
    MonitorAction,
    OperationKind,
    QueueMode,
    ReadinessState,
    RecoveryResolution,
    RobotAvailabilityState,
    RobotPhase,
    WorkUnitState,
)
ENUM_VALUES = frozenset(item.value for enum in _ENUMS for item in enum) | frozenset(
    (*SETTING_NAMES, "cleaning_mode")
)
# Own non-error codes; tests/test_telemetry_contract.py keeps this exact.
CODES = frozenset(
    [
        "abandoned",
        "attempt_not_observing",
        "awaiting_cleaning_start",
        "awaiting_stop",
        "awaiting_stop_stability",
        "awaiting_terminal_stability",
        "cancel_timeout",
        "cleaning_not_finished",
        "cleaning_start_observed",
        "closed",
        "closing",
        "commit_listener_failed",
        "committed",
        "committing",
        "completion_mode_mismatch",
        "completion_scope_mismatch",
        "dispatch_failed",
        "external_run_interrupted",
        "idle_reset",
        "interrupted_before_start",
        "job_prerequisites",
        "loaded",
        "loading",
        "mismatch",
        "observation_failed",
        "observation_out_of_order",
        "observation_source_mismatch",
        "observation_timeout",
        "observed_mode_mismatch",
        "observed_start_and_stable_normal_end",
        "omitted",
        "online",
        "partial",
        "received",
        "recovery_abandoned",
        "rejected",
        "requested",
        "resumed",
        "return",
        "returned",
        "robot_connection_lost",
        "robot_reported_error",
        "run_correlation_uncertain",
        "run_timeout",
        "runtime_interrupted",
        "runtime_reconciliation_failed",
        "scope_mode_and_success_confirmed",
        "selected",
        "stale",
        "start_timeout",
        "started",
        "stop_not_observed",
        "stop_observed",
        "stopped",
        "timeout",
        "unchanged",
        "view_listener_failed",
        "view_projection_failed",
        "waiting",
    ]
)
COMMANDS = frozenset(
    [
        "create_job",
        "update_job",
        "delete_job",
        "move_job",
        "start_job",
        "cancel_job",
        "retry_job",
        "run_queue",
        "pause_queue",
        "resume_queue",
        "configure_queue",
        "create_room",
        "update_room",
        "disable_room",
        "enable_room",
        "release_room",
        "revoke_room",
        "add_robot",
        "configure_robot",
        "remove_robot",
        "save_template",
        "remove_template",
        "create_job_from_template",
        "reset_template_demand",
        "resolve_recovery",
        "hold_job",
        "renew_job_hold",
        "release_job_hold",
        "release_rooms",
        "revoke_rooms",
        "end_queue",
        "return_robot",
        "configure_job_defaults",
        "save_job_as_template",
        "rename_robot",
    ]
)
_ERROR_TYPES = frozenset(
    [
        "ValueError",
        "TypeError",
        "RuntimeError",
        "OSError",
        "PermissionError",
        "TimeoutError",
        "ValidationError",
        "ConflictError",
        "PlanningError",
        "StaleCommandError",
        "DispatchNotStartedError",
        "StorageIntegrityError",
        "ServiceValidationError",
        "Unauthorized",
        "KeyError",
        "AssertionError",
    ]
)
_IDS = frozenset(
    [
        "job_id",
        "attempt_id",
        "robot_id",
        "run_id",
        "request_id",
        "room_id",
        "work_unit_id",
    ]
)
_NUMBERS = frozenset(["sequence", "commit_id", "runtime_sequence"])
_CODED = frozenset(
    {
        "state",
        "stage",
        "reason",
        "quality",
        "operation",
        "phase",
        "observed_operation",
        "completed_operation",
        "targets",
        "previous_state",
        "monitor_action",
        "monitor_reason",
    }
)
# Three-valued observation facts; None stays visible as unknown.
_FLAGS = frozenset({"cleaning_active", "normal_end", "at_dock", "completion_confirmed"})
_LOCAL = frozenset(
    {
        "timestamp",
        "runtime_id",
        "frames",
        "observed_at",
        "deadline_at",
        "terminal_observed_at",
    }
)
# Semantic fields of repeated events; timestamps never make them unique.
_FINGERPRINT = (
    "state",
    "stage",
    "reason",
    "attempt_id",
    "quality",
    "operation",
    "phase",
    "cleaning_active",
    "normal_end",
    "at_dock",
    "completion_confirmed",
    "observed_operation",
    "completed_operation",
    "faults",
    "targets",
    "previous_state",
    "monitor_action",
    "monitor_reason",
    "terminal_observed_at",
)
_REPEATED = frozenset({"robot_observation", "dispatch_blocked", "availability"})


class LoggingSink:
    """Share a runtime-local pseudonym key between logs and diagnostic exports."""

    def __init__(self, known_values: frozenset[str] = frozenset()) -> None:
        self._key = secrets.token_bytes(32)
        # Adapter values are passed in; infrastructure does not import adapters.
        self._values = ERROR_CODES | ENUM_VALUES | CODES | known_values
        self._last: OrderedDict[str, tuple[str, float, int]] = OrderedDict()

    def pseudonym(self, value: str | None) -> str | None:
        """Return an opaque stable reference without retaining original IDs."""
        if value is None:
            return None
        return (
            "ref_"
            + hmac.new(self._key, value.encode(), hashlib.sha256).hexdigest()[:24]
        )

    def sanitize(self, record: Mapping[str, Scalar]) -> dict[str, Scalar]:
        """Render only known fields; unknown text is never copied into an export."""
        result: dict[str, Scalar] = {"diagnostic_version": DIAGNOSTIC_VERSION}
        observation = record.get("event") == TelemetryEvent.OBSERVATION
        for key, value in record.items():
            if key in _FLAGS:
                if observation or value is not None:
                    result[key] = value if isinstance(value, bool) else None
                continue
            if value is None:
                continue
            if key in _IDS:
                result[key] = self.pseudonym(str(value))
            elif key in _NUMBERS and isinstance(value, int):
                result[key] = value
            elif key == "event":
                result[key] = value if value in TelemetryEvent else "unknown"
            elif key == "command":
                result[key] = value if value in COMMANDS else "unknown"
            elif key == "exception_type":
                result[key] = value if value in _ERROR_TYPES else "ExternalError"
            elif key in _CODED:
                result[key] = value if value in self._values else "redacted"
            elif key == "faults":
                result[key] = ",".join(
                    ":".join(
                        part if part in self._values else "redacted"
                        for part in str(fault).split(":")
                    )
                    for fault in str(value).split(",")
                )
            elif key in _LOCAL:
                # These fields are generated locally, never from device/request data.
                result[key] = value
        return result

    def emit(self, record: Mapping[str, Scalar]) -> None:
        """Write changes immediately and count unchanged repeated observations."""
        event = record.get("event")
        level = logging.DEBUG
        if record.get("exception_type") is not None or event == "internal_error":
            level = logging.ERROR
        elif event in {"lifecycle", "availability"}:
            level = logging.INFO
        if not _LOGGER.isEnabledFor(level):
            return
        clean = self.sanitize(record)
        if event in _REPEATED:
            key = f"{event}:{clean.get('robot_id')}:{clean.get('job_id')}"
            fingerprint = json.dumps(
                {name: clean.get(name) for name in _FINGERPRINT}, sort_keys=True
            )
            now = monotonic()
            old = self._last.pop(key, None)
            if old and old[0] == fingerprint and now - old[1] < 60:
                self._last[key] = (fingerprint, old[1], old[2] + 1)
                return
            clean["suppressed"] = old[2] if old else 0
            self._last[key] = (fingerprint, now, 0)
            if len(self._last) > 512:
                self._last.popitem(last=False)
        _LOGGER.log(
            level, "VOI %s", json.dumps(clean, sort_keys=True, separators=(",", ":"))
        )
