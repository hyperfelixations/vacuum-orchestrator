"""The setup status states facts only; see dev doc "Einrichtungsstatus"."""

from dataclasses import replace
from datetime import UTC, datetime

from custom_components.vacuum_orchestrator.domain.job_defaults import JobDefaults
from custom_components.vacuum_orchestrator.domain.queue import OrchestratorState
from custom_components.vacuum_orchestrator.domain.reach import ReachStatus, RoomReach
from custom_components.vacuum_orchestrator.domain.room_registry import RoomRegistry
from custom_components.vacuum_orchestrator.domain.rooms import Room
from custom_components.vacuum_orchestrator.domain.setup import evaluate_setup

NOW = datetime(2026, 10, 9, 8, tzinfo=UTC)
ROOMS = RoomRegistry(
    {
        "kitchen": Room("kitchen", "Kitchen", area_id="kitchen"),
        "garden": Room("garden", "Garden", area_id="garden"),
        "attic": Room("attic", "Attic", area_id="attic", enabled=False),
    }
)
STATE = replace(OrchestratorState.empty("installation"), room_registry=ROOMS)
REACH = {
    "robot": (
        RoomReach("kitchen", ReachStatus.REACHABLE, ("16",)),
        RoomReach("garden", ReachStatus.AREA_NOT_MAPPED),
    ),
    "other": (RoomReach("kitchen", ReachStatus.NOT_ON_CURRENT_MAP),),
}


def test_a_fresh_installation_has_the_assistant_pending_with_its_facts() -> None:
    status = evaluate_setup(STATE, REACH)

    assert status.assistant_pending
    assert status.robot_ids == ("robot", "other")
    assert status.room_ids == ("kitchen", "garden")
    assert status.unreachable_room_ids == ("garden",)
    assert not status.defaults_configured
    assert (status.grace_seconds, status.start_delay_seconds) == (900, 5)


def test_a_finished_setup_is_no_longer_pending_and_nothing_is_mandatory() -> None:
    finished = replace(
        STATE,
        setup_completed_at=NOW,
        job_defaults=replace(JobDefaults(), configured=True),
    )
    status = evaluate_setup(finished, {})

    assert not status.assistant_pending
    assert status.completed_at == NOW
    assert status.defaults_configured
    assert status.robot_ids == ()
    assert status.unreachable_room_ids == ("kitchen", "garden")
