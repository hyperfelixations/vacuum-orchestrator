"""A waiting job is explained on three axes with one main reason."""

from datetime import UTC, datetime

import pytest

from custom_components.vacuum_orchestrator.domain.dispatching import (
    MOMENTARY_CODES,
    STRUCTURAL_CODES,
    Ineligibility,
)
from custom_components.vacuum_orchestrator.domain.rooms import Room
from custom_components.vacuum_orchestrator.domain.waiting import (
    ROBOT_CODES,
    ROBOT_ORDER,
    Blocker,
    RobotBlockers,
    Robots,
    RobotsState,
    pending,
    robot_blocker,
    robot_blockers,
    room_blocker_code,
    waiting,
)

NOW = datetime(2026, 9, 7, 12, tzinfo=UTC)
READY = Robots(RobotsState.READY, (RobotBlockers("a", ()),))


def _robot(robot_id: str, *codes: str, rooms: tuple[str, ...] = ()) -> RobotBlockers:
    return RobotBlockers(
        robot_id, tuple(Blocker(code, room_ids=rooms) for code in codes)
    )


def test_every_eligibility_reason_has_a_public_code_and_a_place() -> None:
    assert set(ROBOT_CODES) == MOMENTARY_CODES | STRUCTURAL_CODES
    assert set(ROBOT_CODES.values()) <= set(ROBOT_ORDER)
    with pytest.raises(ValueError, match="made_up"):
        Ineligibility("made_up")
    reason = Ineligibility("unmapped_target", room_ids=("studio",), detail="studio")
    assert reason.structural and not Ineligibility("battery_below_minimum").structural
    assert robot_blocker(reason) == Blocker(
        "room_unreachable", room_ids=("studio",), detail="studio"
    )


def test_nothing_to_explain_means_the_job_starts() -> None:
    assert waiting((), READY, ()) is None
    assert waiting((), READY, None) is None
    assert pending(None)


def test_the_job_leads_then_the_robots_then_the_queue() -> None:
    busy = Robots(RobotsState.NO_ROBOT_READY, (_robot("a", "robot_busy"),))
    explained = waiting(
        (Blocker("rooms_in_use"), Blocker("room_not_released", room_ids=("studio",))),
        busy,
        (Blocker("start_delayed", until=NOW), Blocker("queue_idle")),
    )
    assert explained is not None
    assert [item.code for item in explained.job] == [
        "room_not_released",
        "rooms_in_use",
    ]
    assert [item.code for item in explained.queue or ()] == [
        "queue_idle",
        "start_delayed",
    ]
    assert explained.primary.code == "room_not_released"

    robots_first = waiting((), busy, (Blocker("queue_idle"),))
    assert robots_first is not None
    assert (robots_first.primary.code, robots_first.primary.robot_ids) == (
        "robot_busy",
        ("a",),
    )

    alone = waiting((), READY, (Blocker("start_delayed", until=NOW),))
    assert alone is not None and alone.primary.until == NOW


def test_alternatives_between_robots_are_never_merged() -> None:
    door_a = Blocker("requirement_not_satisfied", entity_ids=("binary_sensor.a",))
    door_b = Blocker("requirement_not_satisfied", entity_ids=("binary_sensor.b",))
    robots = Robots(
        RobotsState.NO_ROBOT_READY,
        (RobotBlockers("a", (door_a,)), RobotBlockers("b", (door_b,))),
    )
    assert robots.primary == Blocker("no_robot_ready", robot_ids=("a", "b"))

    both_busy = Robots(
        RobotsState.NO_ROBOT_READY,
        (_robot("a", "robot_busy"), _robot("b", "robot_busy")),
    )
    assert both_busy.primary == Blocker("robot_busy", robot_ids=("a", "b"))


def test_a_robot_names_each_reason_once() -> None:
    """A busy robot that also holds a lease is busy once, not twice."""
    reasons = (Ineligibility("robot_busy"), Ineligibility("robot_already_executing"))
    blockers = robot_blockers("a", map(robot_blocker, reasons))
    assert blockers == RobotBlockers("a", (Blocker("robot_busy"),))


def test_unsuitable_robots_explain_rooms_they_miss() -> None:
    together = Robots(
        RobotsState.NO_CAPABLE_ROBOT,
        unsuitable=(
            _robot("a", "room_unreachable", rooms=("studio",)),
            _robot("b", "room_unreachable", rooms=("library",)),
        ),
    )
    assert together.primary == Blocker(
        "rooms_not_reachable_together",
        room_ids=("studio", "library"),
        robot_ids=("a", "b"),
    )
    nowhere = Robots(
        RobotsState.NO_CAPABLE_ROBOT,
        unsuitable=(
            _robot("a", "room_unreachable", rooms=("attic", "studio")),
            _robot("b", "room_unreachable", rooms=("library", "attic")),
        ),
    )
    assert nowhere.primary == Blocker(
        "room_unreachable", room_ids=("attic",), robot_ids=("a", "b")
    )
    mixed = Robots(
        RobotsState.NO_CAPABLE_ROBOT,
        unsuitable=(
            _robot("a", "operation_unsupported"),
            _robot("b", "room_unreachable", rooms=("studio",)),
        ),
    )
    assert mixed.primary == Blocker("no_capable_robot", robot_ids=("a", "b"))
    assert Robots(RobotsState.NO_ROBOT_CONFIGURED).primary == Blocker(
        "no_robot_configured"
    )


def test_only_holds_the_delay_and_the_queue_leave_a_job_pending() -> None:
    held = waiting(
        (Blocker("being_edited", until=NOW),),
        READY,
        (Blocker("start_delayed", until=NOW), Blocker("queue_paused")),
    )
    assert pending(held)
    busy = Robots(RobotsState.NO_ROBOT_READY, (_robot("a", "robot_busy"),))
    assert not pending(waiting((), busy, (Blocker("start_delayed", until=NOW),)))
    assert not pending(waiting((Blocker("room_not_released"),), READY, ()))


def test_a_release_alone_admits_only_an_active_room() -> None:
    room = Room("studio", "Studio", area_id="studio")
    assert room_blocker_code(room) == "room_not_released"
    assert room_blocker_code(Room("studio", "Studio", enabled=False)) == "room_disabled"
    assert room_blocker_code(Room("studio", "Studio", area_missing=True)) == (
        "room_area_missing"
    )
