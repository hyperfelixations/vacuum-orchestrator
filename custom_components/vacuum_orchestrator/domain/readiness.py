"""Fail-closed job-readiness policy independent of robot availability."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .intents import JobIntent
from .types import ReadinessState

_UNKNOWN_STATES = frozenset({"unknown", "unavailable"})


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    """Explainable point-in-time evaluation of one job's prerequisites."""

    state: ReadinessState
    failed_on: tuple[str, ...] = ()
    failed_off: tuple[str, ...] = ()
    unknown: tuple[str, ...] = ()


class ReadinessEvaluator:
    """Evaluate required-on/off references through one shared policy."""

    def evaluate(
        self, intent: JobIntent, states: Mapping[str, str | None]
    ) -> ReadinessReport:
        """Return ready only when every reference is known and satisfied."""
        normalized = {
            reference: value.lower() if value is not None else None
            for reference, value in states.items()
        }
        unknown = tuple(
            reference
            for reference in (*intent.required_on, *intent.required_off)
            if normalized.get(reference) is None
            or normalized.get(reference) in _UNKNOWN_STATES
        )
        if unknown:
            return ReadinessReport(ReadinessState.UNKNOWN, unknown=unknown)
        failed_on = tuple(
            reference
            for reference in intent.required_on
            if normalized.get(reference) != "on"
        )
        failed_off = tuple(
            reference
            for reference in intent.required_off
            if normalized.get(reference) != "off"
        )
        if failed_on or failed_off:
            return ReadinessReport(
                ReadinessState.BLOCKED,
                failed_on=failed_on,
                failed_off=failed_off,
            )
        return ReadinessReport(ReadinessState.READY)
