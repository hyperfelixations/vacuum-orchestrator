"""Ordered setting ladders and job defaults copied into jobs at creation."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from custom_components.vacuum_orchestrator.domain.dispatching import (
    RobotObservation,
    RobotSelector,
)
from custom_components.vacuum_orchestrator.domain.errors import (
    PlanningError,
    ValidationError,
)
from custom_components.vacuum_orchestrator.domain.intents import (
    CleaningPreferences,
    JobIntent,
    JobIntentPatch,
    TargetRef,
)
from custom_components.vacuum_orchestrator.domain.job_defaults import JobDefaults
from custom_components.vacuum_orchestrator.domain.planning import (
    Planner,
    ResolvedSetting,
    SettingsResolution,
)
from custom_components.vacuum_orchestrator.domain.queue import OrchestratorState
from custom_components.vacuum_orchestrator.domain.types import (
    ROUTE_LADDER,
    VACUUM_LADDER,
    WATER_LADDER,
    CleaningMode,
    JobState,
    MopRoute,
    RobotAvailabilityState,
    SettingsPolicy,
    VacuumLevel,
    WaterLevel,
    nearest_supported,
)
from tests.domain.test_planning import _profile

NOW = datetime(2026, 10, 5, 12, tzinfo=UTC)
KITCHEN = (TargetRef("kitchen"),)


@pytest.mark.parametrize(
    ("requested", "supported", "expected"),
    [
        (VacuumLevel.MAXIMUM_PLUS, {VacuumLevel.MAXIMUM}, VacuumLevel.MAXIMUM),
        (VacuumLevel.HIGH, {VacuumLevel.STANDARD, VacuumLevel.MAXIMUM}, "tie_lower"),
        (VacuumLevel.LOW, {VacuumLevel.MAXIMUM, VacuumLevel.HIGH}, VacuumLevel.HIGH),
        (VacuumLevel.LOW, set(), None),
    ],
)
def test_nearest_supported_rung_prefers_the_lower_one_on_a_tie(
    requested: VacuumLevel, supported: set[VacuumLevel], expected: object
) -> None:
    result = nearest_supported(requested, VACUUM_LADDER, frozenset(supported))
    assert result == (VacuumLevel.STANDARD if expected == "tie_lower" else expected)


def test_ladders_are_ordered_and_exclude_off() -> None:
    assert [item.value for item in VACUUM_LADDER] == [
        "low",
        "standard",
        "high",
        "maximum",
        "maximum_plus",
    ]
    assert [item.value for item in WATER_LADDER] == ["low", "medium", "high"]
    assert [item.value for item in ROUTE_LADDER] == [
        "fast",
        "standard",
        "deep",
        "deep_plus",
    ]
    assert (
        nearest_supported(
            WaterLevel.HIGH,
            WATER_LADDER,
            frozenset({WaterLevel.LOW, WaterLevel.MEDIUM}),
        )
        is WaterLevel.MEDIUM
    )


def test_off_is_internal_and_defaults_are_validated() -> None:
    with pytest.raises(ValidationError, match="unsupported_cleaning_preference"):
        CleaningPreferences(vacuum_power=VacuumLevel.OFF)
    with pytest.raises(ValidationError, match="unsupported_cleaning_preference"):
        JobDefaults(mop_intensity=WaterLevel.OFF)
    with pytest.raises(ValidationError, match="invalid_pass_count"):
        JobDefaults(passes=0)


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (CleaningMode.VACUUM, (VacuumLevel.HIGH, None, None)),
        (CleaningMode.MOP, (None, WaterLevel.MEDIUM, MopRoute.STANDARD)),
        (
            CleaningMode.VACUUM_AND_MOP,
            (VacuumLevel.HIGH, WaterLevel.MEDIUM, MopRoute.STANDARD),
        ),
        (
            CleaningMode.VACUUM_THEN_MOP,
            (VacuumLevel.HIGH, WaterLevel.MEDIUM, MopRoute.STANDARD),
        ),
    ],
)
def test_complete_fills_only_the_settings_the_mode_uses(
    mode: CleaningMode, expected: tuple[object, ...]
) -> None:
    intent = JobIntent(KITCHEN, mode, preferences=CleaningPreferences(VacuumLevel.HIGH))
    completed = JobDefaults().complete(intent)
    preferences = completed.preferences
    assert (
        preferences.vacuum_power,
        preferences.mop_intensity,
        preferences.mop_route,
    ) == expected
    assert JobDefaults().complete(completed) is completed


def test_irrelevant_settings_are_dropped_from_the_intent() -> None:
    intent = JobIntent(
        KITCHEN,
        CleaningMode.VACUUM,
        preferences=CleaningPreferences(
            VacuumLevel.LOW, WaterLevel.HIGH, MopRoute.DEEP
        ),
    )
    assert intent.preferences == CleaningPreferences(VacuumLevel.LOW)


def test_queue_copies_defaults_at_creation_and_changes_never_reach_old_jobs() -> None:
    state = OrchestratorState.empty("installation")
    state = state.add_job("a", JobIntent(KITCHEN, CleaningMode.MOP), NOW)
    assert state.jobs["a"].intent.preferences == CleaningPreferences(
        None, WaterLevel.MEDIUM, MopRoute.STANDARD
    )

    state = replace(
        state,
        job_defaults=JobDefaults(
            mop_intensity=WaterLevel.HIGH, mop_route=MopRoute.DEEP, configured=True
        ),
    )
    state = state.add_job("b", JobIntent(KITCHEN, CleaningMode.MOP), NOW)

    assert state.jobs["a"].intent.preferences.mop_intensity is WaterLevel.MEDIUM
    assert state.jobs["b"].intent.preferences == CleaningPreferences(
        None, WaterLevel.HIGH, MopRoute.DEEP
    )


def test_mode_change_fills_newly_used_settings_and_drops_the_rest() -> None:
    state = OrchestratorState.empty("installation").add_job(
        "a",
        JobIntent(
            KITCHEN,
            CleaningMode.VACUUM,
            preferences=CleaningPreferences(VacuumLevel.MAXIMUM),
        ),
        NOW,
    )

    state = state.update_job("a", JobIntentPatch(mode=CleaningMode.MOP), NOW)
    assert state.jobs["a"].intent.preferences == CleaningPreferences(
        None, WaterLevel.MEDIUM, MopRoute.STANDARD
    )
    state = state.update_job("a", JobIntentPatch(mode=CleaningMode.VACUUM_AND_MOP), NOW)
    assert state.jobs["a"].intent.preferences == CleaningPreferences(
        VacuumLevel.STANDARD, WaterLevel.MEDIUM, MopRoute.STANDARD
    )
    state = state.update_job("a", JobIntentPatch(mop_route=None), NOW)
    assert state.jobs["a"].intent.preferences.mop_route is MopRoute.STANDARD


def test_retry_keeps_the_copied_settings() -> None:
    intent = JobIntent(
        KITCHEN, CleaningMode.VACUUM, preferences=CleaningPreferences(VacuumLevel.HIGH)
    )
    state = OrchestratorState.empty("installation").add_job("a", intent, NOW)
    job = state.jobs["a"]
    state = replace(
        state,
        jobs={"a": replace(job, state=JobState.FAILED)},
        queue=(),
        job_defaults=JobDefaults(vacuum_power=VacuumLevel.LOW),
    )
    retried = state.retry_job("a", "b", NOW)
    assert retried.jobs["b"].intent.preferences.vacuum_power is VacuumLevel.HIGH


def _assign(unit, profile, defaults=None):
    observation = RobotObservation(
        profile.robot_id, profile.source_robot_id, RobotAvailabilityState.AVAILABLE
    )
    return RobotSelector().assign(
        unit,
        (profile,),
        {profile.robot_id: observation},
        {},
        frozenset(),
        (),
        defaults=defaults,
    )


def test_records_without_settings_use_the_current_defaults() -> None:
    profile = replace(
        _profile(),
        capabilities=replace(
            _profile().capabilities,
            vacuum_levels=frozenset(VACUUM_LADDER),
        ),
    )
    unit = Planner().create_plan("job", JobIntent(KITCHEN, CleaningMode.VACUUM))
    assignment = _assign(
        unit.work_units[0], profile, JobDefaults(vacuum_power=VacuumLevel.MAXIMUM)
    )
    assert assignment.settings == SettingsResolution(
        (ResolvedSetting("vacuum_power", "maximum", "maximum"),)
    )


def test_strict_policy_never_substitutes_and_unbound_setting_is_not_applicable() -> (
    None
):
    profile = replace(
        _profile(),
        capabilities=replace(
            _profile().capabilities,
            vacuum_levels=frozenset({VacuumLevel.STANDARD, VacuumLevel.MAXIMUM}),
        ),
    )
    intent = JobIntent(
        KITCHEN,
        CleaningMode.VACUUM_AND_MOP,
        preferences=CleaningPreferences(
            VacuumLevel.HIGH, WaterLevel.HIGH, MopRoute.FAST
        ),
    )
    unit = Planner().create_plan("job", intent).work_units[0]

    best = _assign(unit, profile)
    assert best.settings.settings == (
        ResolvedSetting("vacuum_power", "high", "standard"),
        ResolvedSetting("mop_intensity", "high", None),
        ResolvedSetting("mop_route", "fast", None),
    )
    assert best.settings.substituted == ("vacuum_power",)
    assert best.settings.omitted == ("mop_intensity", "mop_route")
    with pytest.raises(PlanningError, match="unsupported_cleaning_preference"):
        _assign(replace(unit, settings_policy=SettingsPolicy.STRICT), profile)


def test_unusable_setting_entity_blocks_every_policy() -> None:
    profile = replace(
        _profile(),
        capabilities=replace(
            _profile().capabilities,
            unavailable_settings=frozenset({"vacuum_power"}),
        ),
    )
    unit = Planner().create_plan("job", JobIntent(KITCHEN, CleaningMode.VACUUM))
    with pytest.raises(PlanningError, match="setting_entity_unavailable"):
        _assign(unit.work_units[0], profile)
