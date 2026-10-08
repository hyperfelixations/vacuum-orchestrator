"""A hold keeps one waiting job from starting until its holder decides."""

from dataclasses import replace
from datetime import timedelta

import pytest

from custom_components.vacuum_orchestrator.domain.errors import (
    ConflictError,
    ValidationError,
)
from custom_components.vacuum_orchestrator.domain.holds import (
    HOLD_LEASE_SECONDS,
    HoldPurpose,
    JobHold,
    lease_end,
)
from custom_components.vacuum_orchestrator.domain.intents import JobIntentPatch
from custom_components.vacuum_orchestrator.domain.queue import OrchestratorState
from custom_components.vacuum_orchestrator.domain.types import JobState
from tests.domain.test_queue import INTENT, NOW, _prepared

LEASE = timedelta(seconds=HOLD_LEASE_SECONDS)
DELAY = timedelta(seconds=5)


def _hold(
    hold_id: str = "h1", job_id: str = "a", purpose: HoldPurpose = HoldPurpose.EDIT
) -> JobHold:
    return JobHold(hold_id, job_id, purpose, NOW, lease_end(NOW))


def _held() -> OrchestratorState:
    state = OrchestratorState.empty("installation").add_job("a", INTENT, NOW)
    return state.hold_job(_hold(), NOW)


def test_one_active_hold_per_waiting_job() -> None:
    state = _held()
    assert state.active_hold("a", NOW) == _hold()
    assert state.commit_id == 2

    with pytest.raises(ConflictError, match="job_held") as raised:
        state.hold_job(_hold("h2", purpose=HoldPurpose.CONFIRM), NOW)
    assert raised.value.detail == "edit"

    later = NOW + LEASE
    assert state.active_hold("a", later) is None
    taken = state.hold_job(
        JobHold("h2", "a", HoldPurpose.CONFIRM, later, lease_end(later)), later
    )
    assert taken.job_holds["a"].hold_id == "h2"


def test_only_waiting_jobs_can_be_held() -> None:
    prepared, _unit = _prepared()
    with pytest.raises(ConflictError, match="job_not_waiting"):
        prepared.hold_job(_hold(), NOW)
    with pytest.raises(ValidationError, match="invalid_job_hold"):
        replace(prepared, job_holds={"a": _hold()})
    with pytest.raises(ValidationError, match="invalid_job_hold"):
        replace(_held(), job_holds={"b": _hold()})
    with pytest.raises(ValidationError, match="invalid_job_hold"):
        JobHold("h1", "a", HoldPurpose.EDIT, NOW, NOW)


def test_the_holder_renews_until_the_lease_lapses() -> None:
    state = _held()
    renewed = state.renew_job_hold("h1", NOW + timedelta(seconds=30))
    assert renewed.job_holds["a"].expires_at == NOW + timedelta(seconds=30) + LEASE
    assert renewed.job_holds["a"].acquired_at == NOW

    for hold_id, at in (("h1", NOW + LEASE), ("other", NOW)):
        with pytest.raises(ConflictError, match="hold_expired"):
            state.renew_job_hold(hold_id, at)


def test_release_ends_the_hold_and_restarts_the_delay() -> None:
    state = _held()
    later = NOW + timedelta(minutes=1)
    released = state.release_job_hold("h1", later)
    assert released.job_holds == {}
    assert released.jobs["a"].start_after == later + DELAY
    assert released.release_job_hold("h1", later) is released


def test_saving_with_the_hold_ends_it_even_without_a_change() -> None:
    state = _held()
    later = NOW + timedelta(minutes=1)
    with pytest.raises(ConflictError, match="job_held"):
        state.update_job("a", JobIntentPatch(note="x"), later)
    with pytest.raises(ConflictError, match="job_held"):
        state.update_job("a", JobIntentPatch(note="x"), later, hold_id="other")

    unchanged = state.update_job("a", JobIntentPatch(), later, hold_id="h1")
    assert unchanged.job_holds == {}
    assert unchanged.jobs["a"].revision == state.jobs["a"].revision
    assert unchanged.jobs["a"].updated_at == NOW
    assert unchanged.jobs["a"].start_after == later + DELAY

    changed = state.update_job("a", JobIntentPatch(note="x"), later, hold_id="h1")
    assert changed.job_holds == {}
    assert changed.jobs["a"].intent.note == "x"
    assert changed.jobs["a"].revision == state.jobs["a"].revision + 1

    with pytest.raises(ConflictError, match="hold_expired"):
        state.update_job("a", JobIntentPatch(note="x"), NOW + LEASE, hold_id="h1")


def test_only_the_holder_deletes_or_cancels_a_held_job() -> None:
    state = _held()
    with pytest.raises(ConflictError, match="job_held"):
        state.delete_job("a", NOW)
    with pytest.raises(ConflictError, match="job_held"):
        state.request_cancel("a", NOW)

    deleted = state.delete_job("a", NOW, hold_id="h1")
    assert "a" not in deleted.jobs and deleted.job_holds == {}

    lapsed = NOW + LEASE
    assert "a" not in state.delete_job("a", lapsed, hold_id="h1").jobs
    cancelled, _generation = state.request_cancel("a", lapsed)
    assert cancelled.jobs["a"].state is JobState.CANCELLED
    assert cancelled.job_holds == {}


def test_lapsed_holds_end_together_in_one_commit() -> None:
    state = OrchestratorState.empty("installation")
    for job_id in ("a", "b", "c"):
        state = state.add_job(job_id, INTENT, NOW)
    state = state.hold_job(_hold("h1", "a"), NOW)
    state = state.hold_job(_hold("h2", "b"), NOW)
    later = NOW + timedelta(seconds=10)
    state = state.hold_job(
        JobHold("h3", "c", HoldPurpose.CONFIRM, later, lease_end(later)), later
    )

    lapsed = NOW + LEASE
    expired = state.expire_job_holds(lapsed)
    assert expired.commit_id == state.commit_id + 1
    assert set(expired.job_holds) == {"c"}
    assert expired.jobs["a"].start_after == expired.jobs["b"].start_after
    assert expired.jobs["a"].start_after == lapsed + DELAY
    assert expired.jobs["c"].start_after == NOW + DELAY
    assert expired.expire_job_holds(lapsed) is expired
