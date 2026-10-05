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
)
from custom_components.vacuum_orchestrator.domain.intents import CleaningPreferences
from custom_components.vacuum_orchestrator.domain.planning import WorkUnit
from custom_components.vacuum_orchestrator.domain.rooms import Room, RoomBinding
from custom_components.vacuum_orchestrator.domain.types import (
    OperationKind,
    PassScope,
    RobotAvailabilityState,
    SemanticLevel,
    SettingsPolicy,
)


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
    await adapter.async_dispatch(work, await assignment(adapter, work))
    assert [call.service for call in calls] == ["send_command"]
    assert calls[0].data == {
        "entity_id": "vacuum.test",
        "command": "app_segment_clean",
        "params": [{"segments": [16, 17], "repeat": 3}],
    }
    assert SemanticLevel.AUTO not in adapter.profile.capabilities.vacuum_levels
    assert SemanticLevel.OFF not in adapter.profile.capabilities.water_levels
    assert adapter.option_mapping("vacuum_levels")["maximum"] == "max_plus"


async def test_map_changes_or_disappearing_segments_reject_the_entire_assignment(
    hass: HomeAssistant,
) -> None:
    adapter, inventory, calls = setup_robot(hass)
    await adapter.async_refresh_maps()
    work = unit()
    planned = await assignment(adapter, work)
    inventory["maps"][0]["rooms"].pop("17")
    with pytest.raises(ConflictError, match="capabilities_changed"):
        await adapter.async_dispatch(work, planned)
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
    await adapter.async_dispatch(work, await assignment(adapter, work))
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
    await adapter.async_dispatch(work, await assignment(adapter, work))
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
        unit(), preferences=CleaningPreferences(mop_intensity=SemanticLevel.LOW)
    )
    with pytest.raises(ConflictError, match="cleaning_mode_not_confirmed"):
        await adapter.async_dispatch(work, await assignment(adapter, work))
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
        await adapter.async_dispatch(work, selected)
    assert not calls
