"""Roborock adapter contracts against public HA entities and actions."""

from dataclasses import replace

import pytest
from homeassistant.components.vacuum.const import VacuumEntityFeature
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers import entity_registry as er

from custom_components.vacuum_orchestrator.adapters.roborock import RoborockAdapter
from custom_components.vacuum_orchestrator.domain.dispatching import RobotSelector
from custom_components.vacuum_orchestrator.domain.errors import (
    ConflictError,
    DispatchNotStartedError,
    PlanningError,
)
from custom_components.vacuum_orchestrator.domain.intents import CleaningPreferences
from custom_components.vacuum_orchestrator.domain.planning import WorkUnit
from custom_components.vacuum_orchestrator.domain.rooms import Room, RoomBinding
from custom_components.vacuum_orchestrator.domain.types import (
    MopRoute,
    OperationKind,
    PassScope,
    RobotAvailabilityState,
    SettingsPolicy,
    VacuumLevel,
    WaterLevel,
)
from tests.adapters.dispatching import dispatch


def setup_robot(hass: HomeAssistant, *, rooms=None, config=None, unbound=()):
    registry = er.async_get(hass)
    vacuum = registry.async_get_or_create(
        "vacuum", "roborock", "unit", suggested_object_id="test"
    )
    registry.async_update_entity_options(
        vacuum.entity_id,
        "vacuum",
        {"area_mapping": {"kitchen": ["0_16", "0_17"], "upstairs": ["1_16"]}},
    )
    roles = {}
    values = {
        "cleaning_mode": (
            "select",
            "vac_and_mop",
            ["vacuum", "mop", "vac_and_mop", "custom", "smart_mode"],
        ),
        "selected_map": ("select", "Ground", ["Ground", "Upper"]),
        "mop_intensity": ("select", "medium", ["off", "low", "medium", "high"]),
        "mop_route": ("select", "standard", ["standard", "deep", "fast"]),
        "status": ("sensor", "charging", None),
        "in_cleaning": ("binary_sensor", "off", None),
        "error": ("sensor", "none", None),
    }
    for role, (domain, value, options) in values.items():
        entity = registry.async_get_or_create(
            domain, "roborock", role, suggested_object_id=role
        )
        roles[role] = entity.id
        hass.states.async_set(
            entity.entity_id, value, {"options": options} if options else {}
        )
    roles.update(dict.fromkeys(unbound))
    hass.states.async_set(
        vacuum.entity_id,
        "docked",
        {
            "supported_features": int(
                VacuumEntityFeature.CLEAN_AREA
                | VacuumEntityFeature.STOP
                | VacuumEntityFeature.SEND_COMMAND
                | VacuumEntityFeature.FAN_SPEED
            ),
            "fan_speed_list": [
                "quiet",
                "balanced",
                "turbo",
                "max",
                "max_plus",
                "off",
                "smart_mode",
            ],
            "fan_speed": "balanced",
        },
    )
    inventory = {
        "maps": [
            {"flag": 0, "name": "Ground", "rooms": {"16": "Kitchen", "17": "Dining"}},
            {"flag": 1, "name": "Upper", "rooms": {"16": "Bedroom"}},
        ]
    }
    calls = []

    async def maps(call: ServiceCall):
        return {vacuum.entity_id: inventory}

    async def command(call: ServiceCall):
        calls.append(call)

    hass.services.async_register(
        "roborock", "get_maps", maps, supports_response=SupportsResponse.ONLY
    )
    hass.services.async_register("vacuum", "send_command", command)
    hass.services.async_register("vacuum", "clean_area", command)
    adapter = RoborockAdapter(
        hass,
        robot_id="robot",
        source_robot_id="physical",
        entity_id=vacuum.entity_id,
        adapter_name="roborock",
        target_areas=("kitchen", "upstairs"),
        configuration={"roles": roles, "protocol": "roborock_v1", **(config or {})},
        rooms=rooms,
    )
    return adapter, inventory, calls


def unit(*, passes=1, targets=("kitchen",)):
    return WorkUnit(
        "unit",
        OperationKind.VACUUM_AND_MOP,
        targets,
        None,
        passes,
        PassScope.TARGET_SET,
        CleaningPreferences(),
        SettingsPolicy.BEST_EFFORT,
        None,
        (),
    )


async def assignment(adapter, work):
    observed = await adapter.async_observe()
    return RobotSelector().assign(
        work, (adapter.profile,), {"robot": observed}, {}, frozenset(), ()
    )


async def test_native_segments_preserve_all_targets_and_repeats(
    hass: HomeAssistant,
) -> None:
    adapter, _, calls = setup_robot(hass)
    await adapter.async_refresh_maps()
    assert adapter.profile.capabilities.target_map == {"kitchen": ("0_16", "0_17")}
    assert adapter.profile.capabilities.operations == frozenset(OperationKind)
    work = unit(passes=3)
    await dispatch(adapter, work, await assignment(adapter, work))
    assert [call.service for call in calls] == ["send_command"]
    assert calls[0].data == {
        "entity_id": "vacuum.test",
        "command": "app_segment_clean",
        "params": [{"segments": [16, 17], "repeat": 3}],
    }
    assert VacuumLevel.OFF not in adapter.profile.capabilities.vacuum_levels
    assert WaterLevel.OFF not in adapter.profile.capabilities.water_levels
    assert adapter.option_mapping("vacuum_levels")["maximum"] == "max"
    assert adapter.option_mapping("vacuum_levels")["maximum_plus"] == "max_plus"


async def test_map_changes_or_disappearing_segments_reject_the_entire_assignment(
    hass: HomeAssistant,
) -> None:
    adapter, inventory, calls = setup_robot(hass)
    await adapter.async_refresh_maps()
    work = unit()
    planned = await assignment(adapter, work)
    inventory["maps"][0]["rooms"].pop("17")
    with pytest.raises(ConflictError, match="capabilities_changed"):
        await dispatch(adapter, work, planned)
    assert calls == []
    assert adapter.profile.capabilities.target_map == {}
    hass.states.async_set(
        "select.selected_map", "Upper", {"options": ["Ground", "Upper"]}
    )
    assert adapter.profile.capabilities.target_map == {"upstairs": ("1_16",)}


async def test_custom_room_uses_explicit_map_scoped_binding(
    hass: HomeAssistant,
) -> None:
    rooms = {
        "custom": Room(
            "custom",
            "Custom",
            bindings=(
                RoomBinding("robot", ("16", "17"), "0"),
                RoomBinding("robot", ("16",), "1"),
            ),
        )
    }
    adapter, _, calls = setup_robot(hass, rooms=lambda: rooms)
    await adapter.async_refresh_maps()
    assert adapter.profile.capabilities.target_map == {"custom": ("0_16", "0_17")}
    work = unit(targets=("custom",))
    await dispatch(adapter, work, await assignment(adapter, work))
    assert calls[0].data["params"] == [{"segments": [16, 17], "repeat": 1}]


@pytest.mark.parametrize(
    "status",
    [
        "mapping",
        "manual_mode",
        "washing_the_mop",
        "going_to_wash_the_mop",
        "emptying_the_bin",
        "returning_home",
        "paused",
    ],
)
async def test_non_floor_activity_is_neither_start_nor_completion(
    hass: HomeAssistant, status: str
) -> None:
    adapter, _, _ = setup_robot(hass)
    hass.states.async_set("sensor.status", status)
    observed = await adapter.async_observe()
    assert observed.state is RobotAvailabilityState.BUSY
    assert observed.cleaning_active is False
    assert observed.normal_end is False


async def test_charge_during_ongoing_cleaning_and_errors_do_not_complete(
    hass: HomeAssistant,
) -> None:
    adapter, _, _ = setup_robot(hass)
    hass.states.async_set("binary_sensor.in_cleaning", "on")
    assert (await adapter.async_observe()).normal_end is False
    hass.states.async_set("binary_sensor.in_cleaning", "off")
    hass.states.async_set("sensor.status", "error")
    observed = await adapter.async_observe()
    assert observed.error_code == "error"
    assert observed.state is RobotAvailabilityState.UNAVAILABLE
    hass.states.async_set("sensor.status", "segment_cleaning")
    assert (await adapter.async_observe()).cleaning_active is True
    hass.states.async_set("vacuum.test", "unavailable")
    assert (await adapter.async_observe()).state is RobotAvailabilityState.UNKNOWN


@pytest.mark.parametrize("status", ["unknown", "unavailable"])
async def test_unusable_bound_status_never_ends_an_active_run(
    hass: HomeAssistant, status: str
) -> None:
    adapter, _, _ = setup_robot(hass)
    hass.states.async_set("binary_sensor.in_cleaning", "on")
    hass.states.async_set("sensor.status", "segment_cleaning")
    assert (await adapter.async_observe()).cleaning_active is True
    hass.states.async_set("sensor.status", status)
    observed = await adapter.async_observe()
    assert observed.state is RobotAvailabilityState.BUSY
    assert observed.normal_end is False
    assert observed.cleaning_active is None
    hass.states.async_set("binary_sensor.in_cleaning", "off")
    observed = await adapter.async_observe()
    assert observed.state is RobotAvailabilityState.BUSY
    assert observed.normal_end is False
    hass.states.async_set("sensor.error", "main_brush_jammed")
    observed = await adapter.async_observe()
    assert observed.state is RobotAvailabilityState.UNAVAILABLE
    assert observed.error_code == "main_brush_jammed"


@pytest.mark.parametrize("flag", ["unknown", "unavailable"])
async def test_unusable_cleaning_flag_blocks_idle_and_normal_end(
    hass: HomeAssistant, flag: str
) -> None:
    adapter, _, _ = setup_robot(hass)
    hass.states.async_set("binary_sensor.in_cleaning", flag)
    observed = await adapter.async_observe()
    assert observed.state is RobotAvailabilityState.BUSY
    assert observed.normal_end is False
    assert observed.cleaning_active is False


async def test_unbound_status_still_honors_the_cleaning_flag(
    hass: HomeAssistant,
) -> None:
    adapter, _, _ = setup_robot(hass, unbound=("status",))
    observed = await adapter.async_observe()
    assert observed.state is RobotAvailabilityState.AVAILABLE
    assert observed.normal_end is True
    hass.states.async_set("binary_sensor.in_cleaning", "on")
    observed = await adapter.async_observe()
    assert observed.state is RobotAvailabilityState.BUSY
    assert observed.normal_end is False
    hass.states.async_set("binary_sensor.in_cleaning", "unavailable")
    assert (await adapter.async_observe()).normal_end is False
    hass.states.async_set("sensor.error", "main_brush_jammed")
    observed = await adapter.async_observe()
    assert observed.state is RobotAvailabilityState.UNAVAILABLE
    assert observed.normal_end is False


async def test_unbound_status_and_flag_keep_the_generic_observation(
    hass: HomeAssistant,
) -> None:
    adapter, _, _ = setup_robot(hass, unbound=("status", "in_cleaning"))
    observed = await adapter.async_observe()
    assert observed.state is RobotAvailabilityState.AVAILABLE
    assert observed.normal_end is True
    hass.states.async_set("vacuum.test", "cleaning")
    observed = await adapter.async_observe()
    assert observed.cleaning_active is True
    assert observed.normal_end is False


async def test_normal_roborock_start_and_end_still_complete(
    hass: HomeAssistant,
) -> None:
    adapter, _, _ = setup_robot(hass)
    hass.states.async_set("binary_sensor.in_cleaning", "on")
    hass.states.async_set("sensor.status", "segment_cleaning")
    observed = await adapter.async_observe()
    assert (observed.cleaning_active, observed.normal_end) == (True, False)
    hass.states.async_set("sensor.status", "returning_home")
    observed = await adapter.async_observe()
    assert (observed.cleaning_active, observed.normal_end) == (False, False)
    hass.states.async_set("binary_sensor.in_cleaning", "off")
    hass.states.async_set("sensor.status", "charging")
    observed = await adapter.async_observe()
    assert observed.state is RobotAvailabilityState.AVAILABLE
    assert (observed.cleaning_active, observed.normal_end) == (False, True)


async def test_start_without_refreshed_inventory_never_sends_segments(
    hass: HomeAssistant,
) -> None:
    adapter, _, calls = setup_robot(hass)
    work = unit()
    await adapter.async_refresh_maps()
    planned = await assignment(adapter, work)
    adapter._maps = None
    with pytest.raises(DispatchNotStartedError, match="map_inventory_unavailable"):
        await adapter.async_start(work, planned)
    assert calls == []


async def test_ambiguous_map_names_and_invalid_inventory_fail_closed(
    hass: HomeAssistant,
) -> None:
    adapter, inventory, calls = setup_robot(hass)
    inventory["maps"][1]["name"] = "Ground"
    await adapter.async_refresh_maps()
    assert adapter.current_map_id is None
    assert adapter.profile.capabilities.target_map == {}
    inventory["maps"][0]["flag"] = True
    with pytest.raises(ConflictError, match="invalid_map_inventory"):
        await adapter.async_refresh_maps()
    assert calls == []


async def test_unnamed_map_matches_ha_fallback_name(hass: HomeAssistant) -> None:
    adapter, inventory, _ = setup_robot(hass)
    inventory["maps"][0]["name"] = None
    hass.states.async_set(
        "select.selected_map", "Map 0", {"options": ["Map 0", "Upper"]}
    )
    await adapter.async_refresh_maps()
    assert adapter.current_map_id == "0"


async def test_non_v1_uses_only_public_area_action_without_native_repeat(
    hass: HomeAssistant,
) -> None:
    adapter, _, calls = setup_robot(hass, config={"protocol": None})
    assert adapter.profile.capabilities.target_map == {}
    er.async_get(hass).async_update_entity_options(
        "vacuum.test", "vacuum", {"area_mapping": {"kitchen": ["16", "17"]}}
    )
    await adapter.async_refresh_maps()
    work = unit()
    await dispatch(adapter, work, await assignment(adapter, work))
    assert calls[0].service == "clean_area"
    assert adapter.profile.capabilities.passes.maximum == 1


async def test_preferences_that_change_mode_block_cleaning(hass: HomeAssistant) -> None:
    adapter, _, calls = setup_robot(hass, config={"water_levels": {"low": "off"}})
    await adapter.async_refresh_maps()

    async def select(call: ServiceCall) -> None:
        hass.states.async_set(
            "select.mop_intensity", "off", {"options": ["off", "low", "medium", "high"]}
        )
        hass.states.async_set(
            "select.cleaning_mode",
            "vacuum",
            {"options": ["vacuum", "mop", "vac_and_mop", "custom", "smart_mode"]},
        )

    hass.services.async_register("select", "select_option", select)
    work = replace(
        unit(), preferences=CleaningPreferences(mop_intensity=WaterLevel.LOW)
    )
    with pytest.raises(ConflictError, match="cleaning_mode_not_confirmed"):
        await dispatch(adapter, work, await assignment(adapter, work))
    assert calls == []


async def test_map_read_failure_is_proven_before_start(
    hass: HomeAssistant, monkeypatch
):
    adapter, _, calls = setup_robot(hass)
    await adapter.async_refresh_maps()
    work = unit()
    selected = await assignment(adapter, work)

    async def failed_read():
        raise RuntimeError("read failed")

    monkeypatch.setattr(adapter, "async_refresh_maps", failed_read)
    with pytest.raises(DispatchNotStartedError, match="map_read_failed"):
        await dispatch(adapter, work, selected)
    assert not calls


_MODE_RESET = {
    "vacuum": ("balanced", "off", "standard"),
    "vac_and_mop": ("balanced", "medium", "standard"),
    "mop": ("off", "medium", "standard"),
}


def simulate_settings(hass: HomeAssistant) -> list[tuple[str, str]]:
    """Behave like Roborock: only a mode change resets the other settings."""
    log: list[tuple[str, str]] = []

    def put(entity_id: str, value: str) -> None:
        state = hass.states.get(entity_id)
        hass.states.async_set(entity_id, value, dict(state.attributes))

    def fan(value: str) -> None:
        state = hass.states.get("vacuum.test")
        hass.states.async_set(
            "vacuum.test", state.state, {**state.attributes, "fan_speed": value}
        )

    async def select(call: ServiceCall) -> None:
        entity_id, option = call.data["entity_id"], call.data["option"]
        log.append((entity_id, option))
        current = hass.states.get(entity_id).state
        if entity_id == "select.cleaning_mode" and current != option:
            speed, water, route = _MODE_RESET[option]
            fan(speed)
            put("select.mop_intensity", water)
            put("select.mop_route", route)
        put(entity_id, option)

    async def set_fan(call: ServiceCall) -> None:
        log.append(("vacuum.test", call.data["fan_speed"]))
        fan(call.data["fan_speed"])

    hass.services.async_register("select", "select_option", select)
    hass.services.async_register("vacuum", "set_fan_speed", set_fan)
    return log


def device_settings(hass: HomeAssistant) -> tuple[str, ...]:
    return (
        hass.states.get("select.cleaning_mode").state,
        hass.states.get("vacuum.test").attributes["fan_speed"],
        hass.states.get("select.mop_intensity").state,
        hass.states.get("select.mop_route").state,
    )


async def test_job_defaults_overwrite_every_setting_of_the_previous_job(
    hass: HomeAssistant,
) -> None:
    adapter, _, _calls = setup_robot(hass)
    await adapter.async_refresh_maps()
    log = simulate_settings(hass)
    first = replace(
        unit(),
        preferences=CleaningPreferences(
            VacuumLevel.MAXIMUM, WaterLevel.HIGH, MopRoute.DEEP_PLUS
        ),
    )
    await dispatch(adapter, first, await assignment(adapter, first))
    # The route select publishes no deep_plus; best effort takes deep.
    assert device_settings(hass) == ("vac_and_mop", "max", "high", "deep")
    log.clear()

    second = replace(unit(), work_unit_id="second")
    await dispatch(adapter, second, await assignment(adapter, second))

    assert device_settings(hass) == ("vac_and_mop", "balanced", "medium", "standard")
    assert log == [
        ("vacuum.test", "balanced"),
        ("select.mop_intensity", "medium"),
        ("select.mop_route", "standard"),
    ]


@pytest.mark.parametrize("previous_mode", ["vacuum", "vac_and_mop"])
async def test_end_state_is_the_same_with_and_without_a_mode_switch(
    hass: HomeAssistant, previous_mode: str
) -> None:
    adapter, _, _calls = setup_robot(hass)
    await adapter.async_refresh_maps()
    log = simulate_settings(hass)
    state = hass.states.get("select.cleaning_mode")
    hass.states.async_set("select.cleaning_mode", previous_mode, state.attributes)
    hass.states.async_set(
        "select.mop_route", "deep", {"options": ["standard", "deep", "fast"]}
    )
    work = replace(
        unit(),
        preferences=CleaningPreferences(
            VacuumLevel.HIGH, WaterLevel.LOW, MopRoute.FAST
        ),
    )

    await dispatch(adapter, work, await assignment(adapter, work))

    assert device_settings(hass) == ("vac_and_mop", "turbo", "low", "fast")
    switched = previous_mode != "vac_and_mop"
    assert (log[0] == ("select.cleaning_mode", "vac_and_mop")) is switched


async def test_fixed_vacuum_mode_switches_unused_water_off(
    hass: HomeAssistant,
) -> None:
    adapter, _, _calls = setup_robot(
        hass, config={"fixed_mode": "vacuum"}, unbound=("cleaning_mode",)
    )
    await adapter.async_refresh_maps()
    log = simulate_settings(hass)
    work = replace(unit(), operation=OperationKind.VACUUM)

    await dispatch(adapter, work, await assignment(adapter, work))

    assert ("select.mop_intensity", "off") in log
    assert hass.states.get("select.mop_intensity").state == "off"


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        (["off", "low", "medium", "high"], ("low", "medium", "high")),
        (["off", "mild", "moderate", "intense"], ("mild", "moderate", "intense")),
        (["off", "mild", "standard", "intense"], ("mild", "standard", "intense")),
    ],
)
def test_water_levels_map_one_to_one_onto_published_options(
    hass: HomeAssistant, options: list[str], expected: tuple[str, ...]
) -> None:
    adapter, _, _calls = setup_robot(hass)
    hass.states.async_set("select.mop_intensity", options[1], {"options": options})
    mapping = adapter.option_mapping("water_levels")
    assert (mapping["low"], mapping["medium"], mapping["high"]) == expected
    assert adapter.profile.capabilities.water_levels == frozenset(
        {WaterLevel.LOW, WaterLevel.MEDIUM, WaterLevel.HIGH}
    )
    assert adapter.option_mapping("mop_routes") == {
        "fast": "fast",
        "standard": "standard",
        "deep": "deep",
    }


async def test_unavailable_setting_entity_never_starts(hass: HomeAssistant) -> None:
    adapter, _, calls = setup_robot(hass)
    await adapter.async_refresh_maps()
    hass.states.async_set("select.mop_route", "unavailable")
    work = unit()
    assert adapter.profile.capabilities.unavailable_settings == frozenset({"mop_route"})
    with pytest.raises(PlanningError, match="setting_entity_unavailable"):
        await assignment(adapter, work)
    assert calls == []


@pytest.mark.parametrize(
    ("status", "in_cleaning", "state", "at_dock"),
    [
        ("charger_disconnected", "off", RobotAvailabilityState.AVAILABLE, False),
        ("charger_disconnected", "on", RobotAvailabilityState.BUSY, False),
        ("idle", "off", RobotAvailabilityState.AVAILABLE, False),
        ("returning_home", "off", RobotAvailabilityState.BUSY, False),
        ("washing_the_mop", "off", RobotAvailabilityState.BUSY, True),
        ("charging", "off", RobotAvailabilityState.AVAILABLE, True),
    ],
)
async def test_rest_and_dock_are_observed_separately(
    hass: HomeAssistant,
    status: str,
    in_cleaning: str,
    state: RobotAvailabilityState,
    at_dock: bool,
) -> None:
    adapter, _, _calls = setup_robot(hass)
    hass.states.async_set("sensor.status", status)
    hass.states.async_set("binary_sensor.in_cleaning", in_cleaning)

    observation = await adapter.async_observe()

    assert (observation.state, observation.at_dock) == (state, at_dock)
    assert observation.normal_end is (state is RobotAvailabilityState.AVAILABLE)


async def test_cancel_with_return_stops_first_and_needs_return_support(
    hass: HomeAssistant,
) -> None:
    adapter, _, _calls = setup_robot(hass)
    sent: list[str] = []

    async def record(call: ServiceCall) -> None:
        sent.append(call.service)

    hass.services.async_register("vacuum", "stop", record)
    hass.services.async_register("vacuum", "return_to_base", record)
    with pytest.raises(ConflictError, match="return_to_dock_unsupported"):
        await adapter.async_cancel(return_to_dock=True)
    assert sent == [] and not adapter.profile.capabilities.returns_to_dock

    state = hass.states.get("vacuum.test")
    hass.states.async_set(
        "vacuum.test",
        state.state,
        {
            **state.attributes,
            "supported_features": state.attributes["supported_features"]
            | VacuumEntityFeature.RETURN_HOME,
        },
    )
    assert adapter.profile.capabilities.returns_to_dock
    await adapter.async_cancel(return_to_dock=True)
    await adapter.async_cancel()
    await adapter.async_return_to_dock()
    assert sent == ["stop", "return_to_base", "stop", "return_to_base"]
