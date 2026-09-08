"""Tests for observed robot-run correlation."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from custom_components.vacuum_orchestrator.domain.correlation import correlate_run
from custom_components.vacuum_orchestrator.domain.execution import (
    ExecutionAttempt,
    RobotRun,
)
from custom_components.vacuum_orchestrator.domain.types import (
    AttemptState,
    CorrelationConfidence,
    CorrelationState,
)

BOUNDARY = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)


def _attempt() -> ExecutionAttempt:
    return ExecutionAttempt(
        attempt_id="attempt-1",
        job_id="job-1",
        work_unit_id="unit-1",
        robot_id="robot-1",
        source_robot_id="roborock:registry-1",
        robot_generation=4,
        state=AttemptState.COMMAND_SENT,
        prepared_at=BOUNDARY - timedelta(seconds=1),
        command_boundary_at=BOUNDARY,
        prior_history_start=BOUNDARY - timedelta(hours=1),
        prior_history_end=BOUNDARY - timedelta(minutes=30),
    )


def test_matching_run_is_strong_evidence_not_identity() -> None:
    run = RobotRun(
        robot_run_id="observed-1",
        source_robot_id="roborock:registry-1",
        observed_start=BOUNDARY + timedelta(seconds=2),
        observed_end=BOUNDARY + timedelta(minutes=10),
        history_start=BOUNDARY + timedelta(seconds=1),
        history_end=BOUNDARY + timedelta(minutes=9),
        cleaning_activity_seen=True,
    )

    correlation = correlate_run(_attempt(), run)

    assert correlation.state is CorrelationState.MATCHED
    assert correlation.confidence is CorrelationConfidence.STRONG
    assert correlation.causal_token_available is False


def test_activity_without_history_remains_uncertain() -> None:
    run = RobotRun(
        robot_run_id="observed-1",
        source_robot_id="roborock:registry-1",
        observed_start=BOUNDARY + timedelta(seconds=2),
        cleaning_activity_seen=True,
    )

    correlation = correlate_run(_attempt(), run)

    assert correlation.state is CorrelationState.UNCERTAIN
    assert correlation.confidence is CorrelationConfidence.WEAK
    assert correlation.requires_attention is True


def test_external_or_contradictory_run_requires_attention() -> None:
    run = RobotRun(
        robot_run_id="observed-external",
        source_robot_id="roborock:registry-1",
        observed_start=BOUNDARY - timedelta(seconds=1),
        history_start=BOUNDARY - timedelta(seconds=1),
        history_end=BOUNDARY + timedelta(minutes=5),
        cleaning_activity_seen=True,
    )

    correlation = correlate_run(_attempt(), run)

    assert correlation.state is CorrelationState.EXTERNAL_OR_CONFLICTING
    assert correlation.confidence is CorrelationConfidence.CONFLICTING
    assert correlation.requires_attention is True


def test_prepared_attempt_cannot_own_observed_run() -> None:
    attempt = replace(_attempt(), state=AttemptState.PREPARED, command_boundary_at=None)

    correlation = correlate_run(
        attempt,
        RobotRun("run-1", "roborock:registry-1", cleaning_activity_seen=True),
    )

    assert correlation.state is CorrelationState.EXTERNAL_OR_CONFLICTING
    assert "attempt_command_not_sent" in correlation.reason_codes


def test_no_evidence_remains_none_confidence() -> None:
    correlation = correlate_run(_attempt(), RobotRun("run-1", "roborock:registry-1"))

    assert correlation.confidence is CorrelationConfidence.NONE
    assert correlation.reason_codes == ("no_run_evidence",)
    assert correlation.requires_attention is True


def test_all_contradictory_evidence_is_reported() -> None:
    run = RobotRun(
        "run-1",
        "other-source",
        observed_start=BOUNDARY - timedelta(seconds=1),
        history_start=BOUNDARY - timedelta(hours=1),
        history_end=BOUNDARY - timedelta(hours=2),
    )

    correlation = correlate_run(_attempt(), run)

    assert set(correlation.reason_codes) >= {
        "source_robot_mismatch",
        "observed_start_before_command",
        "history_start_before_command",
        "history_start_not_advanced",
        "history_end_before_start",
    }
