"""Waiting reasons rank by a fixed priority; the start delay only leads alone."""

from datetime import UTC, datetime

from custom_components.vacuum_orchestrator.domain.waiting import (
    PRIORITY,
    Blocker,
    rank,
    robot_blocker_code,
)

NOW = datetime(2026, 9, 7, 12, tzinfo=UTC)


def test_blockers_rank_by_priority_and_merge_equal_codes() -> None:
    assert rank(()) is None
    waiting = rank(
        (
            Blocker("start_delayed", until=NOW),
            Blocker("robot_busy", robot_ids=("b",)),
            Blocker("room_not_released", room_ids=("hall",)),
            Blocker("robot_busy", robot_ids=("a", "b")),
            Blocker("room_not_released", room_ids=("kitchen", "hall")),
        )
    )
    assert waiting is not None
    assert [item.code for item in waiting.blockers] == [
        "room_not_released",
        "robot_busy",
        "start_delayed",
    ]
    assert waiting.primary.room_ids == ("hall", "kitchen")
    assert waiting.blockers[1].robot_ids == ("b", "a")


def test_the_start_delay_leads_only_when_nothing_else_blocks() -> None:
    assert PRIORITY[-1] == "start_delayed"
    alone = rank((Blocker("start_delayed", until=NOW),))
    assert alone is not None and alone.primary.until == NOW
    behind = rank((Blocker("start_delayed", until=NOW), Blocker("queue_idle")))
    assert behind is not None and behind.primary.code == "queue_idle"


def test_robot_refusals_map_to_waiting_codes() -> None:
    assert robot_blocker_code("robot_already_executing") == "robot_busy"
    assert robot_blocker_code("robot_availability_unknown") == "robot_unavailable"
    assert robot_blocker_code("battery_below_minimum") == "battery_low"
    assert robot_blocker_code("robot_needs_attention") == "robot_needs_attention"
    assert robot_blocker_code("unsupported_pass_count") == "robot_unsuitable"
