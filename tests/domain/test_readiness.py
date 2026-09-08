"""Tests for the fail-closed, robot-independent readiness evaluator."""

from custom_components.vacuum_orchestrator.domain.intents import JobIntent, TargetRef
from custom_components.vacuum_orchestrator.domain.readiness import ReadinessEvaluator
from custom_components.vacuum_orchestrator.domain.types import (
    CleaningMode,
    ReadinessState,
)


def _intent() -> JobIntent:
    return JobIntent(
        (TargetRef("kitchen"),),
        CleaningMode.VACUUM,
        required_on=("binary_sensor.door",),
        required_off=("binary_sensor.person",),
    )


def test_readiness_reports_ready_blocked_and_unknown() -> None:
    evaluator = ReadinessEvaluator()

    ready = evaluator.evaluate(
        _intent(), {"binary_sensor.door": "on", "binary_sensor.person": "off"}
    )
    blocked = evaluator.evaluate(
        _intent(), {"binary_sensor.door": "off", "binary_sensor.person": "on"}
    )
    unknown = evaluator.evaluate(
        _intent(),
        {"binary_sensor.door": "unavailable", "binary_sensor.person": "off"},
    )

    assert ready.state is ReadinessState.READY
    assert blocked.failed_on == ("binary_sensor.door",)
    assert blocked.failed_off == ("binary_sensor.person",)
    assert unknown.state is ReadinessState.UNKNOWN
    assert unknown.unknown == ("binary_sensor.door",)
