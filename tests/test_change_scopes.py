"""Views are notified only for real changes and name the affected scopes."""

from __future__ import annotations

from dataclasses import replace
from types import MappingProxyType

import pytest
from homeassistant.components.vacuum.const import VacuumEntityFeature
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vacuum_orchestrator.const import (
    CONF_ADAPTER,
    CONF_INSTALLATION_ID,
    CONF_ROBOT_ENTITY_ID,
    CONF_ROBOT_REGISTRY_ID,
    CONF_TARGET_AREAS,
    DOMAIN,
)
from custom_components.vacuum_orchestrator.domain.intents import JobIntent, TargetRef
from custom_components.vacuum_orchestrator.domain.queue import OrchestratorState
from custom_components.vacuum_orchestrator.domain.releases import ReleaseKind
from custom_components.vacuum_orchestrator.domain.types import CleaningMode, JobState
from custom_components.vacuum_orchestrator.runtime import (
    async_setup_orchestrator,
    async_unload_orchestrator,
)
from tests.application.test_orchestrator import (
    NOW,
    RecordingAdapter,
    RecordingBackend,
    _intent,
    _orchestrator,
)
from tests.test_runtime import MemoryBackend


def test_job_counters_follow_job_states() -> None:
    state = OrchestratorState.empty("installation").add_job(
        "a", JobIntent((TargetRef("kitchen"),), CleaningMode.VACUUM), NOW
    )
    jobs = dict(state.jobs)
    jobs["b"] = replace(jobs["a"], job_id="b", state=JobState.RUNNING)
    jobs["c"] = replace(jobs["a"], job_id="c", state=JobState.CANCELING)
    jobs["d"] = replace(jobs["a"], job_id="d", state=JobState.NEEDS_ATTENTION)
    counted = replace(state, jobs=jobs, queue=("a",))

    assert counted.active_job_count == 2
    assert counted.attention_job_count == 1


async def test_commits_report_the_changed_scopes() -> None:
    backend = RecordingBackend()
    core = await _orchestrator(backend, RecordingAdapter(backend, "robot"))
    seen: list[frozenset[str]] = []
    core.subscribe_view(lambda: seen.append(core.changed_scopes))

    job = await core.async_create_job(_intent())
    await core.rooms.async_grant("hall", ReleaseKind.PERMANENT)
    await core.async_start_job(job)

    assert seen[0] == {"jobs", "queue"}
    assert seen[1] == {"rooms"}
    assert {"jobs", "queue", "robots", "rooms"} <= set().union(*seen[2:])
    seen.clear()
    await core.async_configure_job_defaults({"passes": 2})
    await core.async_configure_job_defaults({"passes": 2})
    assert seen == [frozenset({"queue"})]


async def test_controller_skips_passes_without_visible_change(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    MemoryBackend.data = None
    monkeypatch.setattr(
        "custom_components.vacuum_orchestrator.runtime.HomeAssistantSnapshotBackend",
        MemoryBackend,
    )
    registry = er.async_get(hass)
    vacuum = registry.async_get_or_create(
        "vacuum", "demo", "unit", suggested_object_id="test"
    )
    area = ar.async_get(hass).async_create("Kitchen")
    registry.async_update_entity_options(
        vacuum.entity_id, "vacuum", {"area_mapping": {area.id: ["16"]}}
    )
    attrs = {"supported_features": int(VacuumEntityFeature.CLEAN_AREA)}
    hass.states.async_set(vacuum.entity_id, "docked", {**attrs, "battery_level": 80})
    hass.states.async_set("binary_sensor.door", "off")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_INSTALLATION_ID: DOMAIN, "auto_discover_robots": False},
    )
    entry.add_to_hass(hass)
    hass.config_entries.async_add_subentry(
        entry,
        ConfigSubentry(
            data=MappingProxyType(
                {
                    CONF_ROBOT_REGISTRY_ID: vacuum.id,
                    CONF_ROBOT_ENTITY_ID: vacuum.entity_id,
                    CONF_ADAPTER: "home_assistant",
                    CONF_TARGET_AREAS: [area.id],
                    "fixed_mode": "vacuum",
                }
            ),
            subentry_type="robot",
            title="Robot",
            unique_id=vacuum.id,
        ),
    )
    await async_setup_orchestrator(hass, entry)
    await hass.async_block_till_done()
    core = entry.runtime_data.orchestrator
    await core.async_create_job(
        JobIntent(
            (TargetRef(area.id),),
            CleaningMode.VACUUM,
            required_on=("binary_sensor.door",),
        )
    )
    await hass.async_block_till_done()
    sequence = core.runtime_sequence

    hass.states.async_set(vacuum.entity_id, "docked", {**attrs, "battery_level": 79})
    await hass.async_block_till_done()
    assert core.runtime_sequence == sequence

    hass.states.async_set("binary_sensor.door", "on")
    await hass.async_block_till_done()
    assert core.runtime_sequence == sequence + 1
    assert core.changed_scopes == {"jobs", "queue"}
    await async_unload_orchestrator(hass, entry)
