"""Faults stop only the operations they concern; see dev doc "Gerätefehler"."""

from dataclasses import replace

from custom_components.vacuum_orchestrator.domain.dispatching import (
    RobotObservation,
    eligibility,
)
from custom_components.vacuum_orchestrator.domain.faults import (
    Fault,
    FaultScope,
    FaultSource,
    blocking,
    interrupting,
)
from custom_components.vacuum_orchestrator.domain.intents import JobIntent, TargetRef
from custom_components.vacuum_orchestrator.domain.job_defaults import JobDefaults
from custom_components.vacuum_orchestrator.domain.planning import Planner, WorkUnit
from custom_components.vacuum_orchestrator.domain.types import (
    CleaningMode,
    OperationKind,
    RobotAvailabilityState,
)
from tests.domain.test_planning import _profile

VACUUM, MOP, BOTH = (
    OperationKind.VACUUM,
    OperationKind.MOP,
    OperationKind.VACUUM_AND_MOP,
)


def fault(scope: FaultScope) -> Fault:
    return Fault(scope.value, FaultSource.ROBOT, scope)


def test_each_scope_stops_its_operations() -> None:
    assert {scope: fault(scope).operations for scope in FaultScope} == {
        FaultScope.GENERAL: {VACUUM, MOP, BOTH},
        FaultScope.VACUUM: {VACUUM, BOTH},
        FaultScope.MOP: {MOP, BOTH},
        FaultScope.STATION: {VACUUM, MOP, BOTH},
        FaultScope.STATION_VACUUM: {VACUUM, BOTH},
        FaultScope.STATION_MOP: {MOP, BOTH},
        FaultScope.NOTICE: set(),
    }


def test_a_finished_floor_run_ignores_station_faults_only() -> None:
    faults = tuple(fault(scope) for scope in FaultScope)

    assert [item.scope for item in blocking(faults, MOP)] == [
        FaultScope.GENERAL,
        FaultScope.MOP,
        FaultScope.STATION,
        FaultScope.STATION_MOP,
    ]
    assert [item.scope for item in interrupting(faults, MOP, finished=True)] == [
        FaultScope.GENERAL,
        FaultScope.MOP,
    ]
    assert len(blocking(faults, None)) == len(FaultScope) - 1


def _unit(mode: CleaningMode) -> WorkUnit:
    intent = JobIntent((TargetRef("kitchen", "main"),), mode)
    return Planner().create_plan("job", intent).work_units[0]


def test_a_robot_with_a_mop_fault_still_vacuums() -> None:
    profile = _profile()
    mop = Fault("vibrarise_jammed", FaultSource.ROBOT, FaultScope.MOP)
    observation = RobotObservation(
        profile.robot_id,
        profile.source_robot_id,
        RobotAvailabilityState.AVAILABLE,
        faults=(mop,),
    )

    def reasons(mode: CleaningMode) -> list[tuple[str, str | None]]:
        return [
            (item.code, item.detail)
            for item in eligibility(
                _unit(mode), profile, observation, {}, frozenset(), JobDefaults()
            )
        ]

    assert reasons(CleaningMode.VACUUM) == []
    assert reasons(CleaningMode.MOP) == [("robot_fault", "vibrarise_jammed")]
    # A general fault explains the unavailability; it is not named twice.
    stuck = replace(
        observation,
        state=RobotAvailabilityState.UNAVAILABLE,
        faults=(Fault("wheels_jammed", FaultSource.ROBOT, FaultScope.GENERAL),),
    )
    assert [
        item.code
        for item in eligibility(
            _unit(CleaningMode.VACUUM), profile, stuck, {}, frozenset(), JobDefaults()
        )
    ] == ["robot_fault"]
