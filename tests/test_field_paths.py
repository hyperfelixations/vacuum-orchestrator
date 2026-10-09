"""Every input error names the field it is about as a dotted path."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.components.vacuum.const import VacuumEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er

from tests.test_configuration_api import call, configured  # noqa: F401


async def located(hass: HomeAssistant, action: str, **data: Any) -> tuple[str, str]:
    """Return the code and the field of the error an action raises."""
    with pytest.raises(ServiceValidationError) as info:
        await call(hass, action, **data)
    placeholders = info.value.translation_placeholders or {}
    return str(info.value.translation_key), placeholders["field"]


@pytest.mark.usefixtures("configured")
async def test_job_errors_name_their_field(hass: HomeAssistant) -> None:
    room = (await call(hass, "create_room", name="Office"))["room_id"]

    assert await located(hass, "create_job", areas=[room], name="  ") == (
        "empty_name",
        "name",
    )
    assert await located(hass, "create_job", areas=[room, "missing"]) == (
        "unknown_room",
        "areas.1",
    )
    assert await located(
        hass,
        "create_job",
        areas=[room],
        required_on=["binary_sensor.door"],
        required_off=["binary_sensor.door"],
    ) == ("contradictory_state_requirement", "required_off")
    await call(hass, "create_job", areas=[room], dedupe_key="daily")
    assert await located(hass, "create_job", areas=[room], dedupe_key="daily") == (
        "dedupe_key_already_queued",
        "dedupe_key",
    )
    assert await located(
        hass, "save_template", name="Daily", intent={"areas": [room], "note": " "}
    ) == ("empty_note", "intent.note")


@pytest.mark.usefixtures("configured")
async def test_room_errors_name_their_field(hass: HomeAssistant) -> None:
    room = (await call(hass, "create_room", name="Office"))["room_id"]

    assert await located(
        hass, "update_room", room_id=room, configuration={"area_id": "nowhere"}
    ) == ("unknown_area", "configuration.area_id")
    assert await located(
        hass,
        "update_room",
        room_id=room,
        configuration={
            "requirements": [
                {"entity_id": "binary_sensor.door", "entity_registry_id": "gone"}
            ]
        },
    ) == ("requirement_binding_missing", "configuration.requirements.0")


@pytest.mark.usefixtures("configured")
async def test_robot_errors_name_their_field(hass: HomeAssistant) -> None:
    vacuum = er.async_get(hass).async_get_or_create("vacuum", "demo", "paths")
    hass.states.async_set(
        vacuum.entity_id,
        "docked",
        {"supported_features": int(VacuumEntityFeature.CLEAN_AREA)},
    )

    assert await located(
        hass, "add_robot", configuration={"robot_entity_id": "vacuum.none"}
    ) == ("entity_not_registered", "configuration.robot_entity_id")
    assert await located(
        hass,
        "add_robot",
        configuration={"robot_entity_id": vacuum.entity_id, "minimum_battery": 150},
    ) == ("invalid_minimum_battery", "configuration.minimum_battery")
    assert await located(
        hass,
        "add_robot",
        configuration={
            "robot_entity_id": vacuum.entity_id,
            "roles": {"battery": "sensor.none"},
        },
    ) == ("invalid_role_entity", "configuration.roles.battery")
    assert await located(
        hass,
        "add_robot",
        configuration={"robot_entity_id": vacuum.entity_id, "target_areas": [" "]},
    ) == ("invalid_target_areas", "configuration.target_areas.0")
    assert await located(
        hass,
        "add_robot",
        configuration={"robot_entity_id": vacuum.entity_id, "colour": "red"},
    ) == ("unknown_robot_configuration_field", "configuration.colour")
    assert await located(
        hass,
        "add_robot",
        configuration={
            "robot_entity_id": vacuum.entity_id,
            "requirements": [{"entity_id": "not an entity"}],
        },
    ) == ("invalid_requirements", "configuration.requirements.0.entity_id")
    assert await located(
        hass,
        "add_robot",
        configuration={
            "robot_entity_id": vacuum.entity_id,
            "start_timeout_seconds": 100000,
        },
    ) == ("timeout_out_of_range", "configuration.start_timeout_seconds")
