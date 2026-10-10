"""Optional diagnostic output; never a source of execution decisions."""

from collections.abc import Callable, Mapping
from contextvars import ContextVar
from enum import StrEnum
from typing import Protocol

Scalar = str | int | bool | None


class TelemetryEvent(StrEnum):
    """Stable event codes shared by the application and adapter boundary."""

    JOB = "job_transition"
    ATTEMPT = "attempt_transition"
    OBSERVATION = "robot_observation"
    BLOCKED = "dispatch_blocked"
    RECOVERY = "recovery_resolved"
    COMMAND = "command"
    QUEUE = "queue_run"
    STORAGE = "storage"
    LIFECYCLE = "lifecycle"
    SETTING = "setting"
    PHYSICAL = "physical_command"
    AVAILABILITY = "availability"
    ERROR = "internal_error"


class TelemetrySink(Protocol):
    """Receive a normalized record without retaining mutable application state."""

    def emit(self, record: Mapping[str, Scalar]) -> None:
        """Publish one record; caller isolates sink failures."""


adapter_reporter: ContextVar[
    Callable[[TelemetryEvent, str, str | None], object] | None
] = ContextVar("voi_adapter_reporter", default=None)


def report_adapter(
    event: TelemetryEvent, stage: str, reason: str | None = None
) -> None:
    """Report inside the current attempt without changing physical control flow."""
    reporter = adapter_reporter.get()
    if reporter is not None:
        reporter(event, stage, reason)
