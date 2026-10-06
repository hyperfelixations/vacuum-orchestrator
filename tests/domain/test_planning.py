"""Tests for robot-independent planning and centralized robot selection."""

from dataclasses import replace

import pytest

from custom_components.vacuum_orchestrator.domain.capabilities import (
    AreaAddressing,
    CancelSemantics,
    CompletionEvidence,
    PassCapability,
    RobotCapabilities,
    RobotProfile,
    StartEvidence,
)
from custom_components.vacuum_orchestrator.domain.dispatching import (
    RobotObservation,
    RobotSelector,
    assignment_supports_current_capabilities,
)
from custom_components.vacuum_orchestrator.domain.errors import PlanningError
from custom_components.vacuum_orchestrator.domain.execution import RobotLease
from custom_components.vacuum_orchestrator.domain.intents import (
    CleaningPreferences,
    JobIntent,
    TargetRef,
    VendorExtension,
)
from custom_components.vacuum_orchestrator.domain.planning import (
    Planner,
    ResolvedSetting,
    SettingsResolution,
)
from custom_components.vacuum_orchestrator.domain.types import (
    CleaningMode,
    MopRoute,
    OperationKind,
    PassScope,
    RobotAvailabilityState,
    SettingsPolicy,
    VacuumLevel,
    WaterLevel,
)


def _profile(robot_id: str = "robot-a", *, preference: int = 0) -> RobotProfile:
    capabilities = RobotCapabilities(
        revision=f"caps-{robot_id}",
        operations=frozenset(OperationKind),
        area_addressing=AreaAddressing.HOME_ASSISTANT_AREA,
        target_map={"kitchen": "16", "hall": "17"},
        map_context="main",
        passes=PassCapability(3, PassScope.TARGET_SET),
        vacuum_levels=frozenset({VacuumLevel.HIGH}),
        water_levels=frozenset(),
        cancel=CancelSemantics.STOP,
        start_evidence=frozenset({StartEvidence.ACTIVITY_START_TRANSITION}),
        completion_evidence=frozenset(
            {
                CompletionEvidence.ACTIVITY_TERMINAL_TRANSITION,
                CompletionEvidence.CLEANING_HISTORY_TIMESTAMPS,
            }
        ),
    )
    return RobotProfile(
        robot_id,
        f"source-{robot_id}",
        "fake",
        capabilities,
        preference=preference,
    )


@pytest.mark.parametrize(
    ("mode", "operations"),
    [
        (CleaningMode.VACUUM, (OperationKind.VACUUM,)),
        (CleaningMode.MOP, (OperationKind.MOP,)),
        (CleaningMode.VACUUM_AND_MOP, (OperationKind.VACUUM_AND_MOP,)),
        (
            CleaningMode.VACUUM_THEN_MOP,
            (OperationKind.VACUUM, OperationKind.MOP),
        ),
    ],
)
def test_planner_preserves_four_mode_semantics(
    mode: CleaningMode, operations: tuple[OperationKind, ...]
) -> None:
    plan = Planner().create_plan(
        "job",
        JobIntent((TargetRef("kitchen", "main"),), mode, passes=2),
    )

    assert tuple(unit.operation for unit in plan.work_units) == operations
    assert plan.work_units[0].canonical_targets == ("kitchen",)
    assert plan.work_units[0].pass_scope is PassScope.TARGET_SET
    if len(plan.work_units) == 2:
        assert plan.work_units[1].depends_on == (plan.work_units[0].work_unit_id,)


def test_plan_is_identical_regardless_of_robot_pool() -> None:
    intent = JobIntent((TargetRef("kitchen"),), CleaningMode.VACUUM)
    plan = Planner().create_plan("job", intent)

    assert not hasattr(plan.work_units[0], "robot_id")


def test_selector_chooses_best_available_robot_and_honors_explicit_choice() -> None:
    unit = (
        Planner()
        .create_plan("job", JobIntent((TargetRef("kitchen"),), CleaningMode.VACUUM))
        .work_units[0]
    )
    low = _profile("low")
    high = _profile("high", preference=10)
    observations = {
        "low": RobotObservation(
            "low", "source-low", RobotAvailabilityState.AVAILABLE, 90
        ),
        "high": RobotObservation(
            "high", "source-high", RobotAvailabilityState.AVAILABLE, 20
        ),
    }
    selector = RobotSelector()

    automatic = selector.assign(unit, (low, high), observations, {}, frozenset(), ())
    explicit = selector.assign(
        unit, (low, high), observations, {}, frozenset(), (), "low"
    )

    assert automatic.robot_id == "high"
    assert explicit.robot_id == "low"


def test_selector_enforces_availability_overlap_and_one_job_per_robot() -> None:
    profile = _profile()
    unit = (
        Planner()
        .create_plan("job", JobIntent((TargetRef("kitchen"),), CleaningMode.VACUUM))
        .work_units[0]
    )
    observation = RobotObservation(
        profile.robot_id,
        profile.source_robot_id,
        RobotAvailabilityState.BUSY,
    )
    selector = RobotSelector()

    with pytest.raises(PlanningError, match="robot_busy"):
        selector.assign(
            unit,
            (profile,),
            {profile.robot_id: observation},
            {},
            frozenset(),
            (),
        )
    with pytest.raises(PlanningError, match="target_overlap_active"):
        selector.assign(
            unit,
            (profile,),
            {
                profile.robot_id: replace(
                    observation, state=RobotAvailabilityState.AVAILABLE
                )
            },
            {},
            frozenset(),
            (frozenset({"kitchen"}),),
        )


def test_best_effort_substitutes_nearest_setting_but_strict_rejects() -> None:
    profile = _profile()
    intent = JobIntent(
        (TargetRef("kitchen"),),
        CleaningMode.VACUUM,
        preferences=CleaningPreferences(vacuum_power=VacuumLevel.MAXIMUM),
    )
    unit = Planner().create_plan("job", intent).work_units[0]
    observations = {
        profile.robot_id: RobotObservation(
            profile.robot_id,
            profile.source_robot_id,
            RobotAvailabilityState.AVAILABLE,
        )
    }
    assignment = RobotSelector().assign(
        unit, (profile,), observations, {}, frozenset(), ()
    )

    assert assignment.settings == SettingsResolution(
        (ResolvedSetting("vacuum_power", "maximum", "high"),)
    )
    assert assignment.settings.substituted == ("vacuum_power",)
    strict = replace(unit, settings_policy=SettingsPolicy.STRICT)
    with pytest.raises(PlanningError, match="unsupported_cleaning_preference"):
        RobotSelector().assign(strict, (profile,), observations, {}, frozenset(), ())


def test_capability_revision_is_fenced_and_errors_are_explainable() -> None:
    profile = _profile()
    unit = (
        Planner()
        .create_plan("job", JobIntent((TargetRef("kitchen"),), CleaningMode.VACUUM))
        .work_units[0]
    )
    observation = RobotObservation(
        profile.robot_id,
        profile.source_robot_id,
        RobotAvailabilityState.AVAILABLE,
    )
    assignment = RobotSelector().assign(
        unit, (profile,), {profile.robot_id: observation}, {}, frozenset(), ()
    )

    assert assignment_supports_current_capabilities(assignment, profile)
    changed = replace(
        profile,
        capabilities=replace(profile.capabilities, revision="changed"),
    )
    assert not assignment_supports_current_capabilities(assignment, changed)
    with pytest.raises(PlanningError, match="unknown_robot"):
        RobotSelector().assign(
            unit,
            (profile,),
            {profile.robot_id: observation},
            {},
            frozenset(),
            (),
            "missing",
        )


def test_capability_and_profile_contracts_validate_configuration() -> None:
    profile = _profile()
    with pytest.raises(Exception, match="invalid_pass_capability"):
        PassCapability(0, PassScope.TARGET_SET)
    with pytest.raises(Exception, match="empty_capability_revision"):
        replace(profile.capabilities, revision=" ")
    with pytest.raises(Exception, match="invalid_robot_identity"):
        replace(profile, robot_id="")
    with pytest.raises(Exception, match="invalid_adapter"):
        replace(profile, adapter=" ")
    with pytest.raises(Exception, match="empty_allowed_operations"):
        replace(profile, allowed_operations=frozenset())
    with pytest.raises(Exception, match="invalid_robot_preference"):
        replace(profile, preference=101)

    restricted = replace(profile, allowed_operations=frozenset({OperationKind.MOP}))
    assert restricted.effective_operations == frozenset({OperationKind.MOP})
    assert profile.effective_operations == profile.capabilities.operations


def test_selector_reports_each_capability_and_ownership_blocker() -> None:
    profile = _profile()
    unit = (
        Planner()
        .create_plan(
            "job", JobIntent((TargetRef("kitchen", "main"),), CleaningMode.VACUUM)
        )
        .work_units[0]
    )
    available = RobotObservation(
        profile.robot_id,
        profile.source_robot_id,
        RobotAvailabilityState.AVAILABLE,
    )
    selector = RobotSelector()

    with pytest.raises(PlanningError, match="no_robot_configured"):
        selector.assign(unit, (), {}, {}, frozenset(), ())
    with pytest.raises(PlanningError, match="robot_availability_unknown"):
        selector.assign(unit, (profile,), {}, {}, frozenset(), ())
    with pytest.raises(PlanningError, match="robot_availability_unknown"):
        selector.assign(
            unit,
            (profile,),
            {profile.robot_id: replace(available, source_robot_id="different-source")},
            {},
            frozenset(),
            (),
        )
    with pytest.raises(PlanningError, match="robot_needs_attention"):
        selector.assign(
            unit,
            (profile,),
            {profile.robot_id: available},
            {},
            frozenset({profile.source_robot_id}),
            (),
        )
    with pytest.raises(PlanningError, match="robot_already_executing"):
        selector.assign(
            unit,
            (profile,),
            {profile.robot_id: available},
            {
                profile.source_robot_id: RobotLease(
                    profile.source_robot_id,
                    profile.robot_id,
                    "attempt",
                    unit.work_unit_id,
                    1,
                )
            },
            frozenset(),
            (),
        )
    with pytest.raises(PlanningError, match="unsupported_operation"):
        selector.assign(
            unit,
            (
                replace(
                    profile,
                    allowed_operations=frozenset({OperationKind.MOP}),
                ),
            ),
            {profile.robot_id: available},
            {},
            frozenset(),
            (),
        )
    with pytest.raises(PlanningError, match="unsupported_map_context"):
        selector.assign(
            unit,
            (
                replace(
                    profile,
                    capabilities=replace(profile.capabilities, map_context="upstairs"),
                ),
            ),
            {profile.robot_id: available},
            {},
            frozenset(),
            (),
        )
    with pytest.raises(PlanningError, match="unmapped_target"):
        selector.assign(
            unit,
            (
                replace(
                    profile, capabilities=replace(profile.capabilities, target_map={})
                ),
            ),
            {profile.robot_id: available},
            {},
            frozenset(),
            (),
        )
    with pytest.raises(PlanningError, match="unsupported_pass_count"):
        selector.assign(
            replace(unit, passes=4),
            (profile,),
            {profile.robot_id: available},
            {},
            frozenset(),
            (),
        )
    with pytest.raises(PlanningError, match="unsupported_pass_scope"):
        selector.assign(
            unit,
            (
                replace(
                    profile,
                    capabilities=replace(
                        profile.capabilities,
                        passes=PassCapability(3, PassScope.PER_TARGET),
                    ),
                ),
            ),
            {profile.robot_id: available},
            {},
            frozenset(),
            (),
        )
    with pytest.raises(PlanningError, match="unsupported_cancel_semantics"):
        selector.assign(
            unit,
            (
                replace(
                    profile,
                    capabilities=replace(
                        profile.capabilities, cancel=CancelSemantics.UNSUPPORTED
                    ),
                ),
            ),
            {profile.robot_id: available},
            {},
            frozenset(),
            (),
        )
    with pytest.raises(PlanningError, match="insufficient_start_evidence"):
        selector.assign(
            unit,
            (
                replace(
                    profile,
                    capabilities=replace(
                        profile.capabilities, start_evidence=frozenset()
                    ),
                ),
            ),
            {profile.robot_id: available},
            {},
            frozenset(),
            (),
        )
    with pytest.raises(PlanningError, match="insufficient_completion_evidence"):
        selector.assign(
            unit,
            (
                replace(
                    profile,
                    capabilities=replace(
                        profile.capabilities, completion_evidence=frozenset()
                    ),
                ),
            ),
            {profile.robot_id: available},
            {},
            frozenset(),
            (),
        )
    with pytest.raises(PlanningError, match="unsupported_vendor_extension"):
        selector.assign(
            replace(unit, vendor_extension=VendorExtension("roborock.v1")),
            (profile,),
            {profile.robot_id: available},
            {},
            frozenset(),
            (),
        )


def test_selector_applies_supported_preferences_and_validates_observation() -> None:
    profile = _profile()
    unit = (
        Planner()
        .create_plan(
            "job",
            JobIntent(
                (TargetRef("kitchen"),),
                CleaningMode.VACUUM_AND_MOP,
                preferences=CleaningPreferences(
                    vacuum_power=VacuumLevel.HIGH,
                    mop_route=MopRoute.DEEP,
                ),
            ),
        )
        .work_units[0]
    )
    observation = RobotObservation(
        profile.robot_id,
        profile.source_robot_id,
        RobotAvailabilityState.AVAILABLE,
    )
    result = RobotSelector().assign(
        unit,
        (
            replace(
                profile,
                capabilities=replace(
                    profile.capabilities, mop_routes=frozenset({MopRoute.DEEP})
                ),
            ),
        ),
        {profile.robot_id: observation},
        {},
        frozenset(),
        (),
    )

    assert result.settings.applied == ("vacuum_power", "mop_route")
    with pytest.raises(PlanningError, match="invalid_battery_percentage"):
        RobotObservation("robot", "source", RobotAvailabilityState.AVAILABLE, 101)


@pytest.mark.parametrize(
    "operation,applied",
    [
        (OperationKind.VACUUM, ("vacuum_power",)),
        (OperationKind.MOP, ("mop_intensity", "mop_route")),
    ],
)
def test_phase_only_applies_relevant_preferences(
    operation: OperationKind, applied: tuple[str, ...]
) -> None:
    profile = _profile()
    profile = replace(
        profile,
        capabilities=replace(
            profile.capabilities,
            water_levels=frozenset({WaterLevel.HIGH}),
            mop_routes=frozenset({MopRoute.DEEP}),
        ),
    )
    unit = (
        Planner()
        .create_plan(
            "job",
            JobIntent(
                (TargetRef("kitchen"),),
                CleaningMode.VACUUM_THEN_MOP,
                preferences=CleaningPreferences(
                    VacuumLevel.HIGH, WaterLevel.HIGH, MopRoute.DEEP
                ),
            ),
        )
        .work_units[0]
    )
    observation = RobotObservation(
        profile.robot_id, profile.source_robot_id, RobotAvailabilityState.AVAILABLE
    )
    result = RobotSelector().assign(
        replace(unit, operation=operation),
        (profile,),
        {profile.robot_id: observation},
        {},
        frozenset(),
        (),
    )
    assert result.settings.applied == applied
    assert result.settings.omitted == ()


def test_supported_preferences_outrank_preferred_robot_and_battery_minimum_blocks() -> (
    None
):
    profile = _profile()
    other = replace(
        _profile("other", preference=100),
        capabilities=replace(profile.capabilities, vacuum_levels=frozenset()),
    )
    unit = (
        Planner()
        .create_plan(
            "job",
            JobIntent(
                (TargetRef("kitchen"),),
                CleaningMode.VACUUM,
                preferences=CleaningPreferences(vacuum_power=VacuumLevel.HIGH),
            ),
        )
        .work_units[0]
    )
    observations = {
        item.robot_id: RobotObservation(
            item.robot_id, item.source_robot_id, RobotAvailabilityState.AVAILABLE, 0
        )
        for item in (profile, other)
    }
    result = RobotSelector().assign(
        unit, (other, profile), observations, {}, frozenset(), ()
    )
    assert result.robot_id == profile.robot_id
    with pytest.raises(PlanningError, match="battery_below_minimum"):
        RobotSelector().assign(
            unit,
            (replace(profile, minimum_battery=20),),
            observations,
            {},
            frozenset(),
            (),
        )
    result = RobotSelector().assign(
        unit, (replace(profile, minimum_battery=0),), observations, {}, frozenset(), ()
    )
    assert result.robot_id == profile.robot_id
