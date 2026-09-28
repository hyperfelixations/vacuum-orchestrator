"""Contract tests for the public Home Assistant vacuum adapter."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from homeassistant.components.vacuum.const import VacuumEntityFeature
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import entity_registry as er

from custom_components.vacuum_orchestrator.adapters.home_assistant_vacuum import (
    HomeAssistantVacuumAdapter,
)
from custom_components.vacuum_orchestrator.adapters.settings import async_set_option
from custom_components.vacuum_orchestrator.application.robot_session import RobotSession
from custom_components.vacuum_orchestrator.domain.errors import (
    ConflictError,
    StaleCommandError,
)
from custom_components.vacuum_orchestrator.domain.intents import CleaningPreferences
from custom_components.vacuum_orchestrator.domain.planning import (
    DispatchAssignment,
    PreferenceResolution,
    WorkUnit,
)
from custom_components.vacuum_orchestrator.domain.rooms import Room, RoomBinding
from custom_components.vacuum_orchestrator.domain.types import (
    OperationKind,
    PassScope,
    RobotAvailabilityState,
    SemanticLevel,
    SettingsPolicy,
)


@pytest.fixture(autouse=True)
def registered_vacuum(hass: HomeAssistant) -> None:
    er.async_get(hass).async_get_or_create(
        "vacuum", "demo", "test", suggested_object_id="test"
    )


def _adapter(hass: HomeAssistant) -> HomeAssistantVacuumAdapter:
    registry = er.async_get(hass)
    vacuum = registry.async_get_or_create(
        "vacuum", "demo", "test", suggested_object_id="test"
    )
    registry.async_update_entity_options(
        vacuum.entity_id, "vacuum", {"area_mapping": {"kitchen": ["segment"]}}
    )
    return HomeAssistantVacuumAdapter(
        hass,
        robot_id="robot",
        source_robot_id="source",
        entity_id="vacuum.test",
        adapter_name="roborock",
        target_areas=("kitchen",),
        configuration={"fixed_mode": "vacuum_and_mop"},
        last_clean_start_entity_id="sensor.start",
        last_clean_end_entity_id="sensor.end",
    )


def _unit() -> WorkUnit:
    return WorkUnit(
        "unit",
        OperationKind.VACUUM_AND_MOP,
        ("kitchen",),
        None,
        1,
        PassScope.TARGET_SET,
        CleaningPreferences(),
        SettingsPolicy.BEST_EFFORT,
        None,
        (),
    )


async def test_dispatch_uses_public_clean_area_with_late_assignment(
    hass: HomeAssistant,
) -> None:
    calls: list[ServiceCall] = []

    async def handle(call: ServiceCall) -> None:
        calls.append(call)

    hass.services.async_register("vacuum", "clean_area", handle)
    hass.states.async_set(
        "vacuum.test",
        "docked",
        {
            "supported_features": int(VacuumEntityFeature.CLEAN_AREA),
            "battery_level": 75,
        },
    )
    hass.states.async_set("sensor.start", "2026-09-07T10:00:00+00:00")
    hass.states.async_set("sensor.end", "2026-09-07T10:10:00+00:00")
    adapter = _adapter(hass)
    unit = _unit()
    assignment = DispatchAssignment(
        "unit",
        "robot",
        "source",
        "roborock",
        ("kitchen",),
        adapter.profile.capabilities.revision,
        PreferenceResolution((), ()),
    )

    await adapter.async_dispatch(unit, assignment)
    observation = await adapter.async_observe()

    assert calls[0].data == {
        "entity_id": "vacuum.test",
        "cleaning_area_id": ["kitchen"],
    }
    assert adapter.profile.capabilities.operations == frozenset(
        {OperationKind.VACUUM_AND_MOP}
    )
    assert observation.state is RobotAvailabilityState.AVAILABLE
    assert observation.battery_percentage == 75
    assert observation.history_start == datetime(2026, 9, 7, 10, tzinfo=UTC)
    assert adapter.watched_entity_ids == (
        "vacuum.test",
        "sensor.start",
        "sensor.end",
    )


async def test_busy_unknown_cancel_and_missing_features_are_normalized(
    hass: HomeAssistant,
) -> None:
    calls: list[ServiceCall] = []

    async def handle(call: ServiceCall) -> None:
        calls.append(call)

    hass.services.async_register("vacuum", "stop", handle)
    hass.states.async_set(
        "vacuum.test", "cleaning", {"supported_features": int(VacuumEntityFeature.STOP)}
    )
    adapter = _adapter(hass)
    assert (await adapter.async_observe()).state is RobotAvailabilityState.BUSY
    await adapter.async_cancel()
    assert calls[0].data == {"entity_id": "vacuum.test"}
    assert adapter.profile.capabilities.operations == frozenset()

    hass.states.async_set("vacuum.test", "unavailable")
    assert (await adapter.async_observe()).state is RobotAvailabilityState.UNKNOWN


async def test_invalid_assignment_and_empty_targets_are_rejected(
    hass: HomeAssistant,
) -> None:
    adapter = _adapter(hass)
    unit = _unit()
    wrong = DispatchAssignment(
        "other", "robot", "source", "roborock", (), "caps", PreferenceResolution((), ())
    )
    with pytest.raises(ConflictError, match="assignment_work_unit_mismatch"):
        await adapter.async_dispatch(unit, wrong)
    empty = replace_assignment_work_unit(wrong, "unit")
    with pytest.raises(ConflictError, match="empty_adapter_targets"):
        await adapter.async_dispatch(unit, empty)


def replace_assignment_work_unit(
    assignment: DispatchAssignment, work_unit_id: str
) -> DispatchAssignment:
    return DispatchAssignment(
        work_unit_id,
        assignment.robot_id,
        assignment.source_robot_id,
        assignment.adapter,
        assignment.adapter_targets,
        assignment.capability_revision,
        assignment.preference_resolution,
    )


def _assignment(
    adapter: HomeAssistantVacuumAdapter,
    unit: WorkUnit | None = None,
    *,
    applied: tuple[str, ...] = (),
) -> DispatchAssignment:
    unit = unit or _unit()
    return DispatchAssignment(
        unit.work_unit_id,
        "robot",
        "source",
        "roborock",
        ("kitchen",),
        adapter.profile.capabilities.revision,
        PreferenceResolution(applied, ()),
    )


def _ready(hass: HomeAssistant) -> None:
    hass.states.async_set(
        "vacuum.test",
        "docked",
        {
            "supported_features": int(
                VacuumEntityFeature.CLEAN_AREA
                | VacuumEntityFeature.STOP
                | VacuumEntityFeature.FAN_SPEED
            ),
            "fan_speed_list": ["quiet", "balanced"],
            "fan_speed": "balanced",
        },
    )


def test_room_addressing_never_invents_mode_or_unmapped_targets(
    hass: HomeAssistant,
) -> None:
    _ready(hass)
    known = _adapter(hass)
    unknown = HomeAssistantVacuumAdapter(
        hass,
        robot_id="robot",
        source_robot_id="source",
        entity_id="vacuum.test",
        target_areas=("kitchen", "missing"),
    )
    assert unknown.profile.capabilities.operations == frozenset()
    assert unknown.profile.capabilities.target_map == {"kitchen": "kitchen"}
    before = known.profile.capabilities.revision
    er.async_get(hass).async_update_entity_options(
        "vacuum.test", "vacuum", {"area_mapping": {"kitchen": ["different"]}}
    )
    assert known.profile.capabilities.revision != before


async def test_registry_rename_and_deletion_follow_stable_identity(
    hass: HomeAssistant,
) -> None:
    _ready(hass)
    adapter = _adapter(hass)
    registry = er.async_get(hass)
    registry.async_update_entity("vacuum.test", new_entity_id="vacuum.renamed")
    assert adapter.entity_id == "vacuum.renamed"
    registry.async_remove("vacuum.renamed")
    assert adapter.entity_id is None
    assert adapter.profile.capabilities.operations == frozenset()
    assert (await adapter.async_observe()).reason == "entity_missing"


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"operation": OperationKind.VACUUM}, "unsupported_operation"),
        ({"passes": 2}, "unsupported_pass_count"),
        ({"canonical_targets": ("missing",)}, "unmapped_target"),
    ],
)
async def test_preflight_rejects_semantic_changes_before_service_call(
    hass: HomeAssistant, change: dict, reason: str
) -> None:
    _ready(hass)
    adapter = _adapter(hass)
    unit = replace(_unit(), **change)
    with pytest.raises(ConflictError, match=reason):
        await adapter.async_dispatch(unit, _assignment(adapter, unit))


async def test_assignment_revision_and_busy_robot_block_dispatch(
    hass: HomeAssistant,
) -> None:
    _ready(hass)
    adapter = _adapter(hass)
    with pytest.raises(ConflictError, match="capabilities_changed"):
        await adapter.async_dispatch(
            _unit(), replace(_assignment(adapter), capability_revision="old")
        )
    hass.states.async_set(
        "vacuum.test", "returning", dict(hass.states.get("vacuum.test").attributes)
    )
    with pytest.raises(ConflictError, match="robot_not_available"):
        await adapter.async_dispatch(_unit(), _assignment(adapter))
    observation = await adapter.async_observe()
    assert observation.cleaning_active is False
    assert observation.normal_end is False


def _mode_adapter(
    hass: HomeAssistant, *, seconds: float = 1
) -> HomeAssistantVacuumAdapter:
    _adapter(hass)
    mode = er.async_get(hass).async_get_or_create(
        "select", "demo", "mode", suggested_object_id="mode"
    )
    hass.states.async_set(
        mode.entity_id, "Sweep", {"options": ["Sweep", "Both", "Mop"]}
    )
    return HomeAssistantVacuumAdapter(
        hass,
        robot_id="robot",
        source_robot_id="source",
        entity_id="vacuum.test",
        adapter_name="roborock",
        target_areas=("kitchen",),
        configuration={
            "roles": {"cleaning_mode": mode.id},
            "mode_options": {"vacuum": "Sweep", "vacuum_and_mop": "Both", "mop": "Mop"},
            "settings_timeout_seconds": seconds,
            "vacuum_levels": {"low": "quiet"},
        },
    )


async def test_settings_are_acknowledged_in_order_before_start(
    hass: HomeAssistant,
) -> None:
    _ready(hass)
    adapter = _mode_adapter(hass)
    calls = []

    async def select(call: ServiceCall) -> None:
        calls.append("mode")
        hass.states.async_set(
            "select.mode", call.data["option"], {"options": ["Sweep", "Both", "Mop"]}
        )

    async def fan(call: ServiceCall) -> None:
        calls.append("fan")
        attrs = dict(hass.states.get("vacuum.test").attributes)
        attrs["fan_speed"] = call.data["fan_speed"]
        hass.states.async_set("vacuum.test", "docked", attrs)

    async def clean(call: ServiceCall) -> None:
        calls.append("clean")

    hass.services.async_register("select", "select_option", select)
    hass.services.async_register("vacuum", "set_fan_speed", fan)
    hass.services.async_register("vacuum", "clean_area", clean)
    unit = replace(
        _unit(), preferences=CleaningPreferences(vacuum_power=SemanticLevel.LOW)
    )
    await adapter.async_dispatch(
        unit, _assignment(adapter, unit, applied=("vacuum_power",))
    )
    assert calls == ["mode", "fan", "clean"]
    assert (
        await adapter.async_observe()
    ).observed_operation is OperationKind.VACUUM_AND_MOP


async def test_setting_without_observed_acknowledgement_never_starts(
    hass: HomeAssistant,
) -> None:
    _ready(hass)
    adapter = _mode_adapter(hass, seconds=0.01)
    calls = []

    async def record(call: ServiceCall) -> None:
        calls.append(call.service)

    hass.services.async_register("select", "select_option", record)
    hass.services.async_register("vacuum", "clean_area", record)
    with pytest.raises(ConflictError, match="setting_confirmation_timeout"):
        await adapter.async_dispatch(_unit(), _assignment(adapter))
    assert calls == ["select_option"]


async def test_cancel_during_mode_setting_prevents_later_clean_command(
    hass: HomeAssistant,
) -> None:
    _ready(hass)
    adapter = _mode_adapter(hass)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def setting(call: ServiceCall) -> None:
        entered.set()
        await release.wait()
        hass.states.async_set(
            "select.mode", "Both", {"options": ["Sweep", "Both", "Mop"]}
        )

    async def record(call: ServiceCall) -> None:
        calls.append(call.service)

    hass.services.async_register("select", "select_option", setting)
    hass.services.async_register("vacuum", "clean_area", record)
    hass.services.async_register("vacuum", "stop", record)
    session = RobotSession("source")
    ticket = session.reserve("attempt")
    task = asyncio.create_task(
        session.dispatch(
            ticket, lambda: adapter.async_dispatch(_unit(), _assignment(adapter))
        )
    )
    await entered.wait()
    stop_ticket = session.fence(1, needs_attention=False)
    stop = asyncio.create_task(session.cancel(stop_ticket, adapter.async_cancel))
    release.set()
    with pytest.raises(StaleCommandError):
        await task
    await stop
    assert calls == ["stop"]


@pytest.mark.parametrize("battery", [-1, 101, True, "bad", None])
async def test_invalid_battery_does_not_break_observation(
    hass: HomeAssistant, battery: object
) -> None:
    hass.states.async_set("vacuum.test", "docked", {"battery_level": battery})
    adapter = _adapter(hass)
    assert (await adapter.async_observe()).battery_percentage is None


async def test_error_and_unsupported_stop_are_not_normal_completion(
    hass: HomeAssistant,
) -> None:
    hass.states.async_set("vacuum.test", "error")
    adapter = _adapter(hass)
    observed = await adapter.async_observe()
    assert observed.error_code == "device_error"
    assert observed.normal_end is False
    with pytest.raises(ConflictError, match="unsupported_cancel_semantics"):
        await adapter.async_cancel()


def test_canonical_rooms_require_complete_explicit_bindings(
    hass: HomeAssistant,
) -> None:
    _ready(hass)
    _adapter(hass)
    rooms = {
        "auto": Room("auto", "Auto", area_id="kitchen"),
        "custom": Room(
            "custom", "Custom", bindings=(RoomBinding("robot", ("kitchen",)),)
        ),
        "incomplete": Room(
            "incomplete",
            "Incomplete",
            bindings=(RoomBinding("robot", ("kitchen", "missing")),),
        ),
        "wrong-map": Room(
            "wrong-map",
            "Wrong map",
            area_id="kitchen",
            bindings=(RoomBinding("robot", ("16",), "upper"),),
        ),
    }
    adapter = HomeAssistantVacuumAdapter(
        hass,
        robot_id="robot",
        source_robot_id="source",
        entity_id="vacuum.test",
        target_areas=("kitchen",),
        rooms=lambda: rooms,
    )
    assert adapter.profile.capabilities.target_map == {}
    rooms.pop("auto")
    assert adapter.profile.capabilities.target_map == {"custom": ("kitchen",)}


async def test_setting_disappearing_while_waiting_never_acknowledges(
    hass: HomeAssistant,
) -> None:
    hass.states.async_set("select.mode", "old", {"options": ["old", "new"]})

    async def setting(call: ServiceCall) -> None:
        hass.states.async_set("select.mode", "unavailable")

    hass.services.async_register("select", "select_option", setting)
    with pytest.raises(ConflictError, match="setting_entity_unavailable"):
        await async_set_option(hass, "select.mode", "new", confirmation_seconds=1)
    with pytest.raises(ConflictError, match="setting_option_unavailable"):
        await async_set_option(hass, "select.missing", "new", confirmation_seconds=1)


async def test_robot_becoming_busy_during_settings_never_starts(
    hass: HomeAssistant,
) -> None:
    _ready(hass)
    adapter = _mode_adapter(hass)

    async def setting(call: ServiceCall) -> None:
        hass.states.async_set(
            "select.mode", "Both", {"options": ["Sweep", "Both", "Mop"]}
        )
        hass.states.async_set(
            "vacuum.test", "cleaning", dict(hass.states.get("vacuum.test").attributes)
        )

    hass.services.async_register("select", "select_option", setting)
    with pytest.raises(ConflictError, match="robot_not_available"):
        await adapter.async_dispatch(_unit(), _assignment(adapter))


def test_overlapping_physical_segments_are_excluded(hass: HomeAssistant):
    adapter = _adapter(hass)
    registry = er.async_get(hass)
    registry.async_update_entity_options(
        "vacuum.test",
        "vacuum",
        {"area_mapping": {"kitchen": ["16"], "hall": ["16", "17"]}},
    )
    adapter._target_areas = ("kitchen", "hall")
    assert adapter.target_mapping() == {}
    registry.async_update_entity_options(
        "vacuum.test",
        "vacuum",
        {"area_mapping": {"kitchen": ["16"], "hall": ["17"]}},
    )
    assert adapter.target_mapping() == {"kitchen": "kitchen", "hall": "hall"}
