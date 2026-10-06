"""Job draft previews offer only robot-supported values and never commit."""

from dataclasses import replace

from custom_components.vacuum_orchestrator.application.preview import preview_job
from custom_components.vacuum_orchestrator.domain.intents import (
    CleaningPreferences,
    JobIntent,
    TargetRef,
)
from custom_components.vacuum_orchestrator.domain.types import (
    CleaningMode,
    MopRoute,
    OperationKind,
    RobotAvailabilityState,
    SettingsPolicy,
    VacuumLevel,
    WaterLevel,
)
from tests.application.test_orchestrator import (
    RecordingAdapter,
    RecordingBackend,
    _orchestrator,
)


def _robot(backend, robot_id, targets, vacuum=(), water=(), routes=()):
    adapter = RecordingAdapter(backend, robot_id, targets=targets)
    adapter._profile = replace(
        adapter.profile,
        capabilities=replace(
            adapter.profile.capabilities,
            vacuum_levels=frozenset(vacuum),
            water_levels=frozenset(water),
            mop_routes=frozenset(routes),
        ),
    )
    return adapter


async def _fleet():
    backend = RecordingBackend()
    first = _robot(
        backend,
        "a",
        ("kitchen", "hall"),
        vacuum=(VacuumLevel.LOW, VacuumLevel.STANDARD, VacuumLevel.HIGH),
        water=(WaterLevel.MEDIUM,),
    )
    second = _robot(
        backend,
        "b",
        ("kitchen",),
        vacuum=(VacuumLevel.STANDARD, VacuumLevel.MAXIMUM),
        water=(WaterLevel.LOW, WaterLevel.MEDIUM, WaterLevel.HIGH),
        routes=(MopRoute.FAST, MopRoute.STANDARD),
    )
    return await _orchestrator(backend, first, second), first, second


def _options(choice):
    return {value: everywhere for value, everywhere in choice.options}


async def test_draft_without_rooms_offers_the_union_and_marks_partial_support():
    core, _first, _second = await _fleet()
    before = core.state

    preview = await preview_job(core, CleaningMode.VACUUM, CleaningPreferences())

    assert list(preview.settings) == ["vacuum_power"]
    choice = preview.settings["vacuum_power"]
    assert _options(choice) == {
        "low": False,
        "standard": True,
        "high": False,
        "maximum": False,
    }
    assert choice.initial == "standard"
    assert preview.reason == "job_requires_area" and preview.robots == ()
    assert core.state is before


async def test_rooms_narrow_options_and_unsupported_requests_preselect_nearest():
    core, first, _second = await _fleet()
    before = core.state.commit_id
    intent = JobIntent(
        (TargetRef("hall"),),
        CleaningMode.VACUUM,
        preferences=CleaningPreferences(VacuumLevel.MAXIMUM),
    )

    preview = await preview_job(core, intent.mode, intent.preferences, intent=intent)

    choice = preview.settings["vacuum_power"]
    assert _options(choice) == {"low": True, "standard": True, "high": True}
    assert choice.initial == "high"
    assert preview.reason is None
    hall = {item.robot_id: item for item in preview.robots}
    assert hall["a"].reason is None
    assert hall["a"].settings.settings[0].applied == "high"
    assert hall["b"].reason == "unmapped_target"
    assert core.state.commit_id == before and not first.dispatches


async def test_two_phase_draft_lists_every_setting_and_robot_per_phase():
    core, _first, _second = await _fleet()
    await core.async_configure_job_defaults({"mop_route": MopRoute.DEEP})
    intent = JobIntent((TargetRef("kitchen"),), CleaningMode.VACUUM_THEN_MOP)

    preview = await preview_job(core, intent.mode, intent.preferences, intent)

    assert list(preview.settings) == ["vacuum_power", "mop_intensity", "mop_route"]
    route = preview.settings["mop_route"]
    assert _options(route) == {"fast": False, "standard": False}
    assert route.initial == "standard"
    assert _options(preview.settings["mop_intensity"]) == {
        "low": False,
        "medium": True,
        "high": False,
    }
    assert [(item.robot_id, item.operation) for item in preview.robots] == [
        ("a", OperationKind.VACUUM),
        ("b", OperationKind.VACUUM),
        ("a", OperationKind.MOP),
        ("b", OperationKind.MOP),
    ]


async def test_no_capable_robot_offers_the_full_ladder_without_support():
    backend = RecordingBackend()
    core = await _orchestrator(backend, _robot(backend, "a", ("kitchen",)))

    preview = await preview_job(core, CleaningMode.MOP, CleaningPreferences())

    assert _options(preview.settings["mop_intensity"]) == {
        "low": False,
        "medium": False,
        "high": False,
    }
    assert preview.settings["mop_intensity"].initial == "medium"


async def test_start_blockers_match_dispatch():
    core, first, second = await _fleet()
    intent = JobIntent(
        (TargetRef("kitchen"),),
        CleaningMode.VACUUM,
        preferences=CleaningPreferences(VacuumLevel.MAXIMUM),
        settings_policy=SettingsPolicy.STRICT,
    )
    preview = await preview_job(core, intent.mode, intent.preferences, intent, "a")
    assert preview.reason == "unsupported_cleaning_preference"
    assert (
        await preview_job(core, intent.mode, intent.preferences, intent)
    ).reason is None

    for adapter in (first, second):
        adapter.observation = replace(
            adapter.observation, state=RobotAvailabilityState.BUSY
        )
    busy = await preview_job(core, intent.mode, intent.preferences, intent)
    assert busy.reason == "robot_busy"
    assert {item.reason for item in busy.robots} == {"robot_busy"}

    await core.rooms.async_revoke("kitchen")
    blocked = await preview_job(core, intent.mode, intent.preferences, intent)
    assert blocked.reason == "job_blocked"
    assert {item.reason for item in blocked.robots} == {"room_not_released"}


async def test_duplicate_dedupe_key_is_reported_not_raised():
    core, _first, _second = await _fleet()
    intent = JobIntent((TargetRef("kitchen"),), CleaningMode.VACUUM, dedupe_key="daily")
    await core.async_create_job(intent)

    preview = await preview_job(core, intent.mode, intent.preferences, intent)

    assert preview.reason == "dedupe_key_already_queued"
