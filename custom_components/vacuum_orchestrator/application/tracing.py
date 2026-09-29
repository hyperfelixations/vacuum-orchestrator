"""Bounded structured execution traces without device payloads or free text."""

from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from datetime import datetime
from uuid import uuid4

from ..domain.errors import OrchestratorError
from ..ports.telemetry import TelemetryEvent as TraceEvent
from ..ports.telemetry import TelemetrySink

__all__ = ["TraceEvent", "TraceRecorder"]

_request_id: ContextVar[str | None] = ContextVar("voi_trace_request", default=None)


@dataclass(frozen=True, slots=True)
class TraceRecord:
    """Allowlisted evidence fields; no names, notes, maps or vendor objects."""

    sequence: int
    timestamp: str
    event: str
    job_id: str | None = None
    attempt_id: str | None = None
    robot_id: str | None = None
    state: str | None = None
    reason: str | None = None
    quality: str | None = None
    runtime_id: str | None = None
    commit_id: int | None = None
    runtime_sequence: int | None = None
    run_id: str | None = None
    request_id: str | None = None
    room_id: str | None = None
    work_unit_id: str | None = None
    command: str | None = None
    stage: str | None = None
    operation: str | None = None
    exception_type: str | None = None
    frames: str | None = None


class TraceRecorder:
    """Keep a bounded runtime trace independently of critical execution storage."""

    def __init__(
        self, capacity: int = 512, *, sink: TelemetrySink | None = None
    ) -> None:
        self._records: deque[TraceRecord] = deque(maxlen=capacity)
        self.sequence = 0
        self.sink_failures = 0
        self.sink = sink
        self.runtime_id = str(uuid4())
        self.commit_id: int | None = None
        self.runtime_sequence = 0
        self.run_id: str | None = None

    @contextmanager
    def request(self) -> Iterator[str]:
        """Keep concurrent requests distinct across asynchronous suspension."""
        request_id = str(uuid4())
        token = _request_id.set(request_id)
        try:
            yield request_id
        finally:
            _request_id.reset(token)

    def record(
        self,
        event: TraceEvent,
        now: datetime,
        *,
        job_id: str | None = None,
        attempt_id: str | None = None,
        robot_id: str | None = None,
        state: str | None = None,
        reason: str | None = None,
        quality: str | None = None,
        room_id: str | None = None,
        work_unit_id: str | None = None,
        command: str | None = None,
        stage: str | None = None,
        operation: str | None = None,
        error: Exception | None = None,
    ) -> None:
        """Append only normalized values chosen by the application."""
        self.sequence += 1
        if reason is None and isinstance(error, OrchestratorError):
            reason = error.code
        frames: list[str] = []
        if error is not None:
            traceback = error.__traceback__
            while traceback is not None and len(frames) < 12:
                code = traceback.tb_frame.f_code
                path = code.co_filename.replace("\\", "/")
                marker = "custom_components/vacuum_orchestrator/"
                frame = path.split(marker, 1)[1] if marker in path else "external"
                frames.append(f"{frame}:{traceback.tb_lineno}")
                traceback = traceback.tb_next
        record = TraceRecord(
            self.sequence,
            now.isoformat(),
            event.value,
            job_id,
            attempt_id,
            robot_id,
            state,
            reason,
            quality,
            self.runtime_id,
            self.commit_id,
            self.runtime_sequence,
            self.run_id,
            _request_id.get(),
            room_id,
            work_unit_id,
            command,
            stage,
            operation,
            type(error).__name__ if error is not None else None,
            ";".join(frames) if frames else None,
        )
        self._records.append(record)
        if self.sink is not None:
            try:
                self.sink.emit(asdict(record))
            except Exception:
                self.sink_failures += 1

    def snapshot(self, job_id: str | None = None) -> list[dict[str, str | int | None]]:
        """Return independent records, optionally scoped to one job."""
        return [
            asdict(item)
            for item in self._records
            if job_id is None or item.job_id == job_id
        ]
