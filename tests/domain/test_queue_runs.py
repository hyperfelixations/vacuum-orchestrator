"""The run phase follows one decision table; see dev doc "Laufphase"."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from custom_components.vacuum_orchestrator.domain.queue_runs import (
    QueueRun,
    RunPhase,
    run_phase,
)
from custom_components.vacuum_orchestrator.domain.types import QueueMode

NOW = datetime(2026, 9, 7, 12, tzinfo=UTC)
RUN = QueueRun("run", NOW)
ENDING = replace(RUN, end_requested_at=NOW)
CLOSED = replace(RUN, idle_since=NOW, completed_at=NOW)


@pytest.mark.parametrize(
    ("mode", "run", "has_work", "phase"),
    [
        (QueueMode.IDLE, None, False, RunPhase.OFF),
        (QueueMode.IDLE, CLOSED, True, RunPhase.OFF),
        (QueueMode.IDLE, RUN, True, RunPhase.OFF),
        (QueueMode.RUNNING, RUN, True, RunPhase.ACTIVE),
        (QueueMode.RUNNING, RUN, False, RunPhase.STANDBY),
        (QueueMode.PAUSED, RUN, True, RunPhase.PAUSED),
        (QueueMode.PAUSED, RUN, False, RunPhase.PAUSED),
        (QueueMode.PAUSED, ENDING, True, RunPhase.ENDING),
        (QueueMode.RUNNING, ENDING, False, RunPhase.ENDING),
    ],
)
def test_the_phase_follows_the_decision_table(mode, run, has_work, phase) -> None:
    assert run_phase(mode, run, has_work=has_work) is phase
