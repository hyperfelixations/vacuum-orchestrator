"""Anonymized diagnostics built exclusively from allowlisted read models."""

from itertools import islice
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import API_VERSION, INTEGRATION_VERSION, STORE_VERSION
from .runtime import VacuumOrchestratorRuntime


def build_diagnostics(runtime: VacuumOrchestratorRuntime) -> dict[str, Any]:
    """Anonymize identifiers consistently within one export; exclude configuration."""
    core = runtime.orchestrator
    tokens: dict[str, str] = {}

    def anonymize(value: str | None) -> str | None:
        if value is None:
            return None
        if value not in tokens:
            tokens[value] = f"ref_{len(tokens) + 1}"
        return tokens[value]

    state = core.state
    traces = [
        {
            key: anonymize(str(value))
            if key.endswith("_id") and value is not None
            else value
            for key, value in record.items()
        }
        for record in core.trace.snapshot()
    ]
    return {
        "version": INTEGRATION_VERSION,
        "api_version": API_VERSION,
        "store_version": STORE_VERSION,
        "commit_id": state.commit_id,
        "runtime_sequence": core.runtime_sequence,
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
                "failure_code": job.failure_code,
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
    return build_diagnostics(entry.runtime_data)
