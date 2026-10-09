"""Check a command's input like the command does, without any effect.

See internal dev doc "Validierung".
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import probatio
from homeassistant.core import HomeAssistant

from ..configuration import prepare_robot
from ..configuration_values import schema_path
from ..const import API_VERSION
from ..domain.errors import OrchestratorError, ValidationError, located
from ..runtime import async_get_runtime
from .job_input import (
    CREATE_JOB_SCHEMA,
    UPDATE_SCHEMA,
    intent_from_data,
    patch_from_data,
)

Schema = Callable[[dict[str, Any]], dict[str, Any]]
Execute = Callable[[HomeAssistant, str, dict[str, Any]], Awaitable[object]]


async def async_validate(
    hass: HomeAssistant,
    command: str,
    data: dict[str, Any],
    *,
    schemas: Mapping[str, Schema],
    execute: Execute,
) -> dict[str, Any]:
    """Return every schema error, or else the first error the command raises.

    `schemas` and `execute` are the configuration commands' own.
    """
    jobs: dict[str, Schema] = {
        "create_job": CREATE_JOB_SCHEMA,
        "update_job": UPDATE_SCHEMA,
    }
    schema = {**jobs, **schemas}[command]
    errors: list[OrchestratorError] = []
    try:
        values = schema(data)
    except probatio.MultipleInvalid as err:
        errors = [schema_error(item) for item in err.errors]
    else:
        try:
            await _check(hass, command, values, execute)
        except OrchestratorError as err:
            errors = [err]
    return {
        "api_version": API_VERSION,
        "valid": not errors,
        "errors": [{"path": list(item.path), "code": item.code} for item in errors],
    }


def schema_error(err: probatio.Invalid) -> ValidationError:
    """Name one schema error by a stable code at its field."""
    path = schema_path(err)
    if isinstance(err, probatio.RequiredFieldInvalid):
        return ValidationError("required_field", path=path)
    if isinstance(err, probatio.ExtraKeysInvalid):
        return ValidationError("unknown_field", path=path)
    if isinstance(err, (probatio.RangeInvalid, probatio.LengthInvalid)):
        return ValidationError("out_of_range", path=path)
    return ValidationError("invalid_value", path=path)


async def _check(
    hass: HomeAssistant, command: str, values: dict[str, Any], execute: Execute
) -> None:
    runtime = async_get_runtime(hass)
    core = runtime.orchestrator
    controller = runtime.controller
    assert controller is not None
    if command == "create_job":
        # Start conditions are no field errors: check the creation only.
        intent = intent_from_data(
            values,
            core.active_room_ids,
            core.state.job_defaults,
            core.entity_references,
        )
        await core.async_dry_run(core.async_create_job(intent))
    elif command == "update_job":
        await core.async_dry_run(
            core.async_update_job(
                values["job_id"],
                patch_from_data(values, core.active_room_ids, core.entity_references),
                hold_id=values.get("hold_id"),
            )
        )
    elif command in {"add_robot", "configure_robot"}:
        with located("configuration"):
            prepare_robot(
                hass,
                controller.entry,
                values["configuration"],
                robot_id=values.get("robot_id"),
            )
    elif command == "rename_robot":
        controller.checked_name(values["robot_id"], values.get("name"))
    else:
        await core.async_dry_run(execute(hass, command, values))
