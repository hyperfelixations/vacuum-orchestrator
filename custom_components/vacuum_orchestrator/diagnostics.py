"""Anonymized diagnostics built exclusively from allowlisted read models."""

import re
from itertools import islice
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .adapters.public_values import ADAPTER_VALUES
from .const import API_VERSION, INTEGRATION_VERSION, STORE_VERSION
from .infrastructure.telemetry import LoggingSink
from .runtime import TELEMETRY_KEY, VacuumOrchestratorRuntime, async_get_registry

# Vendor segment IDs carry no names; HA area IDs do and are pseudonymized.
_SEGMENT = re.compile(r"\d+(?:_\d+)?")


def build_diagnostics(runtime: VacuumOrchestratorRuntime) -> dict[str, Any]:
    """Anonymize identifiers consistently within one export; exclude configuration."""
    core = runtime.orchestrator
    sink = core.trace.sink
    sanitizer = sink if isinstance(sink, LoggingSink) else LoggingSink(ADAPTER_VALUES)
    anonymize = sanitizer.pseudonym

    state = core.state
    traces = [sanitizer.sanitize(record) for record in core.trace.snapshot()]
    return {
        "version": INTEGRATION_VERSION,
        "api_version": API_VERSION,
        "store_version": STORE_VERSION,
        "commit_id": state.commit_id,
        "runtime_sequence": core.runtime_sequence,
        "runtime_id": core.runtime_id,
        "sink_failures": core.trace.sink_failures,
        "trace_window": {
            "recorded": core.trace.sequence,
            "retained": len(traces),
            "dropped": core.trace.sequence - len(traces),
        },
        "mode": state.mode.value,
        "needs_attention": state.needs_attention,
        "totals": {"jobs": len(state.jobs), "rooms": len(state.room_registry.rooms)},
        "export_limit": 500,
        "jobs": [
            {
                "job_id": anonymize(job.job_id),
                "state": job.state.value,
                "mode": job.intent.mode.value,
                "room_ids": [anonymize(item.area_id) for item in job.intent.areas],
                "failure_code": sanitizer.sanitize({"reason": job.failure_code}).get(
                    "reason"
                ),
            }
            for job in islice(state.jobs.values(), 500)
        ],
        "robots": [
            {
                "robot_id": anonymize(profile.robot_id),
                "operations": sorted(
                    item.value for item in profile.effective_operations
                ),
                "blocked": profile.source_robot_id in state.blocked_robots,
                "reach": [
                    {
                        "room_id": anonymize(item.room_id),
                        "status": item.status.value,
                        "targets": len(item.targets),
                        "ignored": [
                            value if _SEGMENT.fullmatch(value) else anonymize(value)
                            for value in item.ignored
                        ],
                    }
                    for item in adapter.room_reach()
                ],
            }
            for adapter in core.adapters.values()
            for profile in (adapter.profile,)
        ],
        "rooms": [
            {
                "room_id": anonymize(room.room_id),
                "enabled": room.enabled,
                "area_missing": room.area_missing,
                "due_basis": room.due_policy.basis.value,
                "completion_quality": {
                    key.value: stamp.quality.value
                    for key, stamp in room.last_cleaning.items()
                },
            }
            for room in islice(state.room_registry.rooms.values(), 500)
        ],
        "traces": traces,
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Provide HA's diagnostic download without raw config-entry or device data."""
    runtime = async_get_registry(hass).get(entry.entry_id)
    if isinstance(runtime, VacuumOrchestratorRuntime):
        return build_diagnostics(runtime)
    trace = hass.data.get(TELEMETRY_KEY, {}).get(entry.entry_id)
    sanitizer = trace.sink if trace is not None else LoggingSink(ADAPTER_VALUES)
    return {
        "version": INTEGRATION_VERSION,
        "runtime_loaded": False,
        "traces": [sanitizer.sanitize(record) for record in trace.snapshot()]
        if trace is not None
        else [],
    }
