"""Every error code reaching users has an English and German message."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any, cast

from homeassistant.components.websocket_api.connection import ActiveConnection
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.setup import async_setup_component

from custom_components.vacuum_orchestrator.api import websocket as websocket_api
from custom_components.vacuum_orchestrator.api.errors import (
    send_websocket_error,
    service_error,
)
from custom_components.vacuum_orchestrator.const import DOMAIN, SERVICE_GET_JOB
from custom_components.vacuum_orchestrator.domain.errors import ConflictError
from custom_components.vacuum_orchestrator.runtime import RUNTIME_KEY
from tests.test_websocket import Connection

ROOT = Path(__file__).parents[1]
PACKAGE = ROOT / "custom_components" / DOMAIN
ERRORS = {
    "ValidationError",
    "ConflictError",
    "PlanningError",
    "StaleCommandError",
    "DispatchNotStartedError",
    "StorageIntegrityError",
    "OrchestratorError",
}
CODE_ARGUMENT = {"identifier": 1, "_normalized_references": 1}
# Call sites whose code is computed; each maps to every value it can produce.
DYNAMIC = {
    ("adapters/home_assistant_vacuum.py", "DispatchNotStartedError(code)"): {
        "settings_failed",
        "start_rejected",
    },
    ("adapters/roborock.py", "DispatchNotStartedError(code)"): {"map_read_failed"},
    (
        "application/orchestrator.py",
        "PlanningError(f'job_{report.state.value}')",
    ): {"job_blocked", "job_unknown"},
    (
        "application/preview.py",
        "PlanningError(f'job_{report.state.value}')",
    ): {"job_blocked", "job_unknown"},
    ("domain/dispatching.py", "PlanningError(failures[0])"): set(),
    (
        "domain/dispatching.py",
        "PlanningError(failures[0] if len(unique) == 1 else 'no_eligible_robot')",
    ): {"no_eligible_robot"},
    ("domain/dispatching.py", "PlanningError(f'robot_{observation.state.value}')"): {
        "robot_busy",
        "robot_unavailable",
        "robot_unknown",
    },
    ("domain/intents.py", "ValidationError(code)"): set(),
    ("domain/intents.py", "ValidationError(f'empty_{field_name}')"): {
        "empty_name",
        "empty_reason",
        "empty_note",
        "empty_dedupe_key",
    },
    ("domain/validation.py", "ValidationError(code)"): {"invalid_identifier"},
    ("infrastructure/codec.py", "StorageIntegrityError(f'ambiguous_legacy_{name}')"): {
        "ambiguous_legacy_vacuum_level",
        "ambiguous_legacy_water_level",
        "ambiguous_legacy_mop_route",
    },
}
TRANSPORT_CODES = {"invalid_parameters"}


def _source_codes() -> tuple[set[str], set[tuple[str, str]]]:
    codes: set[str] = set()
    dynamic: set[tuple[str, str]] = set()
    for path in PACKAGE.rglob("*.py"):
        relative = path.relative_to(PACKAGE).as_posix()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            name = (
                function.id
                if isinstance(function, ast.Name)
                else function.attr
                if isinstance(function, ast.Attribute)
                else None
            )
            index = 0 if name in ERRORS else CODE_ARGUMENT.get(name or "")
            if index is None or len(node.args) <= index:
                continue
            argument = node.args[index]
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                codes.add(argument.value)
            elif name in ERRORS:
                dynamic.add((relative, ast.unparse(node)))
    return codes, dynamic


def _translations(name: str) -> dict[str, Any]:
    return cast(
        dict[str, Any], json.loads((PACKAGE / name).read_text(encoding="utf-8"))
    )


def test_every_error_code_has_a_message_in_each_language() -> None:
    codes, dynamic = _source_codes()
    assert dynamic == set(DYNAMIC)
    expected = codes | TRANSPORT_CODES | set().union(*DYNAMIC.values())
    for name in ("strings.json", "translations/en.json", "translations/de.json"):
        exceptions = _translations(name)["exceptions"]
        assert set(exceptions) == expected, name
        for code, entry in exceptions.items():
            message = entry["message"]
            assert message.strip() == message and message, (name, code)
            assert "{" not in message and "http" not in message, (name, code)


async def test_actions_raise_translated_errors_with_structured_detail(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    assert await async_setup_component(hass, DOMAIN, {})
    hass.data[RUNTIME_KEY] = {}
    try:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_GET_JOB,
            {"job_id": "missing"},
            blocking=True,
            return_response=True,
        )
    except ServiceValidationError as err:
        error = err
    assert error.translation_domain == DOMAIN
    assert error.translation_key == "orchestrator_not_loaded"
    assert error.translation_placeholders == {
        "code": "orchestrator_not_loaded",
        "detail": "",
    }
    assert str(error) == "Vacuum Orchestrator is not loaded"


async def test_websocket_errors_use_the_same_translation_fields(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    assert await async_setup_component(hass, DOMAIN, {})
    connection = Connection()
    send_websocket_error(
        cast(ActiveConnection, connection), 3, ConflictError("unknown_job", "job-1")
    )
    assert connection.errors == [(3, "unknown_job", "The job does not exist")]
    assert connection.translations == [
        (3, "unknown_job", DOMAIN, {"code": "unknown_job", "detail": "job-1"})
    ]
    error = service_error(ConflictError("unknown_job", "job-1"))
    assert error.translation_placeholders == {"code": "unknown_job", "detail": "job-1"}

    websocket_api.websocket_configuration_get(
        hass,
        cast(ActiveConnection, connection),
        {"id": 4, "query": "get_room", "parameters": {"unexpected": True}},
    )
    await hass.async_block_till_done()
    assert connection.errors[-1][:2] == (4, "invalid_parameters")
    assert connection.translations[-1][1] == "invalid_parameters"
    assert connection.translations[-1][3]["detail"]
