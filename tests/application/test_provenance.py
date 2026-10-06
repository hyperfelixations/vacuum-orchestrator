"""VOI records how each job came into the queue; templates can come from jobs."""

from dataclasses import replace

import pytest

from custom_components.vacuum_orchestrator.domain.errors import (
    ConflictError,
    ValidationError,
)
from custom_components.vacuum_orchestrator.domain.intents import (
    CleaningPreferences,
    JobIntent,
    JobIntentPatch,
    TargetRef,
)
from custom_components.vacuum_orchestrator.domain.queue import (
    JobProvenance,
    OrchestratorState,
)
from custom_components.vacuum_orchestrator.domain.requests import CommandOrigin
from custom_components.vacuum_orchestrator.domain.types import (
    CleaningMode,
    MopRoute,
    ProvenanceKind,
    SettingsPolicy,
    WaterLevel,
)
from custom_components.vacuum_orchestrator.infrastructure.codec import (
    decode_orchestrator_state,
    encode_orchestrator_state,
)
from custom_components.vacuum_orchestrator.ports.command_scope import command_origin
from tests.application.test_orchestrator import NOW, _intent
from tests.application.test_templates import setup_due


@pytest.mark.parametrize(
    ("origin", "kind"),
    [
        (None, ProvenanceKind.MANUAL),
        (CommandOrigin("ctx", "user", None), ProvenanceKind.MANUAL),
        (CommandOrigin("ctx", None, "parent"), ProvenanceKind.AUTOMATION),
    ],
)
def test_new_jobs_are_manual_or_automation_by_request_context(origin, kind) -> None:
    state = OrchestratorState.empty("installation").add_job(
        "job", _intent(), NOW, origin=origin
    )

    assert state.jobs["job"].provenance == JobProvenance(kind)


def test_retry_records_its_own_origin_and_the_retried_job() -> None:
    state = OrchestratorState.empty("installation").add_job(
        "job", _intent(), NOW, provenance=JobProvenance(ProvenanceKind.TEMPLATE, "t")
    )
    cancelled, _ = state.request_cancel("job", NOW)

    retried = cancelled.retry_job("job", "retry", NOW).jobs["retry"]

    assert retried.provenance == JobProvenance(ProvenanceKind.RETRY)
    assert retried.retries_job_id == "job"


@pytest.mark.parametrize(
    ("kind", "template_id"),
    [
        (ProvenanceKind.TEMPLATE, None),
        (ProvenanceKind.AUTOMATIC, None),
        (ProvenanceKind.MANUAL, "t"),
        (ProvenanceKind.RETRY, "t"),
    ],
)
def test_only_template_origins_name_a_template(kind, template_id) -> None:
    with pytest.raises(ValidationError, match="invalid_job_provenance"):
        JobProvenance(kind, template_id)


def test_choosing_rooms_explicitly_ends_the_all_rooms_choice() -> None:
    everything = replace(_intent(), all_rooms=True)

    assert JobIntentPatch(passes=2).apply(everything).all_rooms
    assert not JobIntentPatch(areas=(TargetRef("hall"),)).apply(everything).all_rooms
    assert (
        JobIntentPatch(areas=(TargetRef("hall"),), all_rooms=True)
        .apply(everything)
        .all_rooms
    )


async def test_template_jobs_and_due_jobs_name_their_template() -> None:
    core = await setup_due()
    manual = await core.templates.async_save("Manual", _intent("hall"))
    rule = await core.templates.async_save(
        "Rule", replace(_intent(), reason="weekly"), automatic=True
    )
    token = command_origin.set(CommandOrigin("ctx", None, "parent"))
    try:
        instance = await core.templates.async_create_job(manual)
    finally:
        command_origin.reset(token)
    await core.templates.async_generate_due()

    assert core.state.jobs[instance].provenance == JobProvenance(
        ProvenanceKind.TEMPLATE, manual
    )
    assert core.state.jobs[instance].origin == CommandOrigin("ctx", None, "parent")
    (due,) = (job for job in core.state.jobs.values() if job.job_id != instance)
    assert due.provenance == JobProvenance(ProvenanceKind.AUTOMATIC, rule)
    assert due.intent.reason == "weekly"
    assert due.intent.dedupe_key.startswith(f"due:{rule}:")
    assert decode_orchestrator_state(encode_orchestrator_state(core.state)) == (
        core.state
    )


async def test_job_saved_as_template_keeps_what_was_cleaned_and_how() -> None:
    core = await setup_due()
    intent = JobIntent(
        (TargetRef("kitchen"), TargetRef("hall")),
        CleaningMode.MOP,
        name="Evening",
        preferences=CleaningPreferences(None, WaterLevel.HIGH, MopRoute.DEEP),
        passes=2,
        reason="guests",
        note="Bring the rug up",
        dedupe_key="evening",
        required_off=("binary_sensor.person",),
        settings_policy=SettingsPolicy.STRICT,
        all_rooms=True,
    )
    job = await core.async_create_job(intent)
    await core.async_cancel_job(job)
    before = core.state.commit_id

    key = await core.templates.async_save_job(job, "From job", automatic=True)

    template = core.state.templates[key]
    assert core.state.commit_id == before + 1
    assert (template.name, template.enabled, template.automatic) == (
        "From job",
        True,
        True,
    )
    assert template.intent == replace(intent, reason=None, dedupe_key=None)
    assert decode_orchestrator_state(encode_orchestrator_state(core.state)) == (
        core.state
    )
    with pytest.raises(ConflictError, match="unknown_job"):
        await core.templates.async_save_job("missing", "Missing")
    assert set(core.state.templates) == {key}
