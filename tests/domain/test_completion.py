"""Deviating runs book only evidenced work; see dev doc "Abweichungen"."""

import pytest

from custom_components.vacuum_orchestrator.domain.completion import (
    evidenced_operation,
)
from custom_components.vacuum_orchestrator.domain.types import OperationKind

VACUUM = OperationKind.VACUUM
MOP = OperationKind.MOP
BOTH = OperationKind.VACUUM_AND_MOP


@pytest.mark.parametrize(
    "planned,observed,evidenced",
    [
        (VACUUM, (), VACUUM),
        (BOTH, (), BOTH),
        (VACUUM, (VACUUM, BOTH), VACUUM),
        (MOP, (BOTH,), MOP),
        (BOTH, (BOTH, VACUUM), VACUUM),
        (BOTH, (MOP,), MOP),
        (VACUUM, (VACUUM, MOP), None),
        (BOTH, (VACUUM, MOP), None),
    ],
)
def test_only_what_every_observed_mode_covered_counts(
    planned: OperationKind,
    observed: tuple[OperationKind, ...],
    evidenced: OperationKind | None,
) -> None:
    assert evidenced_operation(planned, observed) is evidenced
