"""HA-compatible diagnostic logging with bounded repetition and private IDs."""

import hashlib
import hmac
import json
import logging
import secrets
from collections import OrderedDict
from collections.abc import Mapping
from time import monotonic

from ..ports.telemetry import Scalar, TelemetryEvent

_LOGGER = logging.getLogger(__name__)
_VALUES = frozenset(
    [
        "queued",
        "dispatching",
        "running",
        "canceling",
        "completed",
        "failed",
        "cancelled",
        "needs_attention",
        "prepared",
        "command_sent",
        "accepted",
        "start_confirmed",
        "settling",
        "succeeded",
        "cancel_pending",
        "recovery_required",
        "available",
        "unavailable",
        "unknown",
        "busy",
        "ready",
        "blocked",
        "idle",
        "paused",
        "derived",
        "confirmed",
        "vacuum",
        "mop",
        "vacuum_and_mop",
        "vacuum_then_mop",
        "received",
        "accepted",
        "rejected",
        "returned",
        "requested",
        "confirmed",
        "skipped",
        "timeout",
        "stale",
        "starting",
        "ready",
        "closing",
        "closed",
        "failed",
        "loading",
        "loaded",
        "committing",
        "committed",
        "migrating",
        "migrated",
        "started",
        "waiting",
        "resumed",
        "paused",
        "completed",
        "idle_reset",
        "online",
        "offline",
        "selected",
        "omitted",
        "stopped",
        "unchanged",
        "vacuum_power",
        "mop_intensity",
        "mop_route",
        "cleaning_mode",
        "low",
        "medium",
        "high",
        "max",
        "off",
        "fast",
        "standard",
        "deep",
        "strict",
        "best_effort",
        "job_prerequisites",
        "job_blocked",
        "job_unknown",
        "job_not_startable",
        "job_not_dispatchable",
        "job_conditions_not_satisfied",
        "no_compatible_robot",
        "robot_not_available",
        "observation_failed",
        "settings_failed",
        "setting_option_unavailable",
        "setting_entity_unavailable",
        "setting_confirmation_timeout",
        "unsupported_operation",
        "cleaning_mode_not_confirmed",
        "cleaning_mode_conflicts_with_fixed_mode",
        "readiness_changed_before_start",
        "capabilities_changed_before_dispatch",
        "operator_assumed_stopped",
        "verified_stopped",
        "runtime_reconciliation_failed",
        "map_inventory_unavailable",
        "map_inventory_available",
        "critical_storage_state_uncertain",
        "critical_commit_missing_after_save",
        "critical_commit_readback_mismatch",
        "critical_commit_semantic_mismatch",
        "installation_storage_ownership_mismatch",
        "invalid_snapshot",
        "snapshot_digest_mismatch",
        "start_timeout",
        "run_timeout",
        "cancel_timeout",
        "robot_error",
        "connection_lost",
        "unknown_job",
        "unknown_robot",
        "unknown_room",
        "room_locked",
        "room_unreleased",
        "room_area_missing",
        "requirement_unknown",
        "requirement_blocked",
        "requirement_stale",
        "room_requirements",
        "robot_requirements",
        "robot_reserved",
        "target_reserved",
        "atomic_writes_required",
        "non_monotonic_commit",
        "legacy_import_verification_failed",
        "dispatch_failed",
        "view_listener_failed",
        "commit_listener_failed",
        "unsupported_cancel_semantics",
        "orchestrator_not_loaded",
        "robot_stopped_confirmation_required",
        "invalid_parameters",
        "unauthorized",
        "error",
    ]
)
_COMMANDS = frozenset(
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
        "remove_room",
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
_REPEATED = frozenset({"robot_observation", "dispatch_blocked", "availability"})


class LoggingSink:
    """Share a runtime-local pseudonym key between logs and diagnostic exports."""

    def __init__(self) -> None:
        self._key = secrets.token_bytes(32)
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
        result: dict[str, Scalar] = {"diagnostic_version": 1}
        for key, value in record.items():
            if value is None:
                continue
            if key in _IDS:
                result[key] = self.pseudonym(str(value))
            elif key in _NUMBERS and isinstance(value, int):
                result[key] = value
            elif key == "event":
                result[key] = value if value in TelemetryEvent else "unknown"
            elif key == "command":
                result[key] = value if value in _COMMANDS else "unknown"
            elif key == "exception_type":
                result[key] = value if value in _ERROR_TYPES else "ExternalError"
            elif key in {"state", "stage", "reason", "quality", "operation"}:
                result[key] = value if value in _VALUES else "redacted"
            elif key in {"timestamp", "runtime_id", "frames"}:
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
                {
                    name: clean.get(name)
                    for name in (
                        "state",
                        "stage",
                        "reason",
                        "attempt_id",
                        "quality",
                        "operation",
                    )
                },
                sort_keys=True,
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
