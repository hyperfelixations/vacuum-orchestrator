"""Telemetry shows own codes; see dev doc "Strukturierte HA-Protokollierung"."""

from __future__ import annotations

import ast

from homeassistant.core import HomeAssistant, SupportsResponse

from custom_components.vacuum_orchestrator.adapters.public_values import (
    ADAPTER_VALUES,
)
from custom_components.vacuum_orchestrator.api.actions import async_setup_actions
from custom_components.vacuum_orchestrator.api.configuration import (
    CARD_COMMANDS,
    COMMANDS,
)
from custom_components.vacuum_orchestrator.const import DOMAIN
from custom_components.vacuum_orchestrator.infrastructure import telemetry
from tests.test_error_translations import (
    DYNAMIC,
    PACKAGE,
    TRANSPORT_CODES,
    _source_codes,
)

# Fields whose values pass the code allowlist, and the calls that set them.
FIELDS = {"reason", "stage", "state", "quality", "operation", "targets"}
TRACING = {"record", "ObservationResult"}
# Calls whose positional argument at this index is a failure code.
FAILURE_ARGUMENT = {"fail_job": 2, "require_robot_attention": 1}


def _strings(node: ast.expr, names: dict[str, set[str]]) -> set[str]:
    """Return the string constants an expression can take."""
    if isinstance(node, ast.Constant):
        return {node.value} if isinstance(node.value, str) else set()
    if isinstance(node, ast.IfExp):
        return _strings(node.body, names) | _strings(node.orelse, names)
    if isinstance(node, ast.BoolOp):
        return set().union(*(_strings(value, names) for value in node.values))
    if isinstance(node, ast.Name):
        return names.get(node.id, set())
    return set()


def _local_strings(function: ast.AST) -> dict[str, set[str]]:
    names: dict[str, set[str]] = {}
    for node in ast.walk(function):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.setdefault(target.id, set()).update(_strings(node.value, {}))
    return names


def _call_name(node: ast.Call) -> str | None:
    function = node.func
    if isinstance(function, ast.Attribute):
        return function.attr
    return function.id if isinstance(function, ast.Name) else None


def _traced_codes() -> set[str]:
    codes: set[str] = set()
    for path in PACKAGE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        scopes = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        ]
        for scope in scopes:
            names = _local_strings(scope)
            for node in ast.walk(scope):
                if isinstance(node, ast.Assign) and any(
                    isinstance(target, ast.Attribute) and target.attr == "last_error"
                    for target in node.targets
                ):
                    codes |= _strings(node.value, names)
                if not isinstance(node, ast.Call):
                    continue
                name = _call_name(node)
                if name == "MonitorDecision" and len(node.args) > 1:
                    codes |= _strings(node.args[1], names)
                if name in FAILURE_ARGUMENT and len(node.args) > FAILURE_ARGUMENT[name]:
                    codes |= _strings(node.args[FAILURE_ARGUMENT[name]], names)
                if name == "report_adapter":
                    for argument in node.args[1:]:
                        codes |= _strings(argument, names)
                for keyword in node.keywords:
                    if keyword.arg == "failure_code" or (
                        name in TRACING and keyword.arg in FIELDS
                    ):
                        codes |= _strings(keyword.value, names)
    raised, _dynamic = _source_codes()
    return codes | raised | set().union(*DYNAMIC.values()) | TRANSPORT_CODES


def test_the_code_allowlist_is_exactly_the_codes_the_package_traces() -> None:
    expected = (
        _traced_codes() - telemetry.ENUM_VALUES - ADAPTER_VALUES - telemetry.ERROR_CODES
    )
    assert sorted(expected - telemetry.CODES) == []
    assert sorted(telemetry.CODES - expected) == []


async def test_the_command_allowlist_is_exactly_the_traced_commands(
    hass: HomeAssistant,
) -> None:
    await async_setup_actions(hass)
    services = {
        name
        for name in hass.services.async_services_for_domain(DOMAIN)
        if hass.services.supports_response(DOMAIN, name) is not SupportsResponse.ONLY
    }
    assert services | COMMANDS.keys() | CARD_COMMANDS.keys() == telemetry.COMMANDS
