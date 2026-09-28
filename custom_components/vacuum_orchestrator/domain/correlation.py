"""Conservative physical-run correlation policy."""

from __future__ import annotations

from datetime import timedelta

from .completion import CompletionQuality
from .execution import ExecutionAttempt, RobotRun, RunCorrelation
from .types import CorrelationConfidence, CorrelationState


def correlate_run(attempt: ExecutionAttempt, run: RobotRun) -> RunCorrelation:
    """Classify evidence without claiming an unavailable causal run identity."""
    reasons: list[str] = []
    if attempt.command_boundary_at is None:
        reasons.append("attempt_command_not_sent")
        boundary = attempt.prepared_at
    else:
        boundary = attempt.command_boundary_at
    if attempt.source_robot_id != run.source_robot_id:
        reasons.append("source_robot_mismatch")
    if (
        run.observed_start is not None
        and run.observed_end is not None
        and run.observed_end < run.observed_start
    ):
        reasons.append("observed_end_before_start")
    if (
        run.history_end is not None
        and run.observed_end is not None
        and run.history_end > run.observed_end + timedelta(seconds=1)
    ):
        reasons.append("history_end_after_observed_end")
    if run.observed_start is not None and run.observed_start < boundary:
        reasons.append("observed_start_before_command")
    if (
        run.history_start is not None
        and run.history_start + timedelta(seconds=1) < boundary
    ):
        reasons.append("history_start_before_command")
    if (
        run.history_start is not None
        and attempt.prior_history_start is not None
        and run.history_start == attempt.prior_history_start
    ):
        reasons.append("history_start_not_advanced")
    if (
        run.history_end is not None
        and attempt.prior_history_end is not None
        and run.history_end == attempt.prior_history_end
    ):
        reasons.append("history_end_not_advanced")
    if (
        run.history_start is not None
        and run.history_end is not None
        and run.history_end < run.history_start
    ):
        reasons.append("history_end_before_start")

    if reasons:
        return RunCorrelation(
            attempt.attempt_id,
            run.robot_run_id,
            CorrelationState.EXTERNAL_OR_CONFLICTING,
            CorrelationConfidence.CONFLICTING,
            tuple(reasons),
            run.causal_token is not None,
        )

    history_pair = run.history_start is not None and run.history_end is not None
    if (
        run.completion_quality is not None
        and run.cleaning_activity_seen
        and run.observed_start is not None
        and run.observed_end is not None
        and run.observed_end >= run.observed_start
    ):
        return RunCorrelation(
            attempt.attempt_id,
            run.robot_run_id,
            CorrelationState.MATCHED,
            CorrelationConfidence.STRONG
            if run.completion_quality is CompletionQuality.CONFIRMED
            else CorrelationConfidence.WEAK,
            (
                "confirmed_completion"
                if run.completion_quality is CompletionQuality.CONFIRMED
                else "derived_completion",
            ),
            run.causal_token is not None,
        )
    if run.cleaning_activity_seen and history_pair:
        return RunCorrelation(
            attempt.attempt_id,
            run.robot_run_id,
            CorrelationState.MATCHED,
            CorrelationConfidence.STRONG,
            ("activity_and_new_history_pair",),
            run.causal_token is not None,
        )
    if run.cleaning_activity_seen or history_pair:
        return RunCorrelation(
            attempt.attempt_id,
            run.robot_run_id,
            CorrelationState.UNCERTAIN,
            CorrelationConfidence.WEAK,
            ("incomplete_run_evidence",),
            run.causal_token is not None,
        )
    return RunCorrelation(
        attempt.attempt_id,
        run.robot_run_id,
        CorrelationState.UNCERTAIN,
        CorrelationConfidence.NONE,
        ("no_run_evidence",),
        run.causal_token is not None,
    )
