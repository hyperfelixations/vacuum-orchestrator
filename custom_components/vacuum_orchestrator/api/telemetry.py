"""Correlate authenticated commands without copying request payloads."""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime

from ..application.tracing import TraceEvent, TraceRecorder
from ..domain.errors import OrchestratorError


@contextmanager
def command_trace(
    trace: TraceRecorder, name: str, references: Mapping[str, object] | None = None
) -> Iterator[None]:
    """Report command return separately from physical completion."""
    with trace.request():
        ids = {
            key: value
            for key in ("job_id", "room_id", "robot_id")
            if isinstance(value := (references or {}).get(key), str)
        }
        trace.record(
            TraceEvent.COMMAND,
            datetime.now(UTC),
            command=name,
            stage="received",
            job_id=ids.get("job_id"),
            room_id=ids.get("room_id"),
            robot_id=ids.get("robot_id"),
        )
        try:
            yield
        except Exception as err:
            cause = err if isinstance(err, OrchestratorError) else err.__cause__
            expected = isinstance(cause, OrchestratorError)
            trace.record(
                TraceEvent.COMMAND,
                datetime.now(UTC),
                command=name,
                stage="rejected" if expected else "failed",
                reason=cause.code if isinstance(cause, OrchestratorError) else None,
                error=None if expected else err,
            )
            raise
        trace.record(
            TraceEvent.COMMAND, datetime.now(UTC), command=name, stage="returned"
        )
