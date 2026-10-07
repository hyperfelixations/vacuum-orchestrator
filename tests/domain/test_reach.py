"""Room reach: one reason per room, overlaps never executable."""

from custom_components.vacuum_orchestrator.domain.reach import (
    ReachStatus,
    RoomReach,
    reachable_targets,
    without_overlaps,
)


def test_rooms_sharing_a_physical_unit_both_lose_their_targets() -> None:
    reach = without_overlaps(
        (
            RoomReach("kitchen", ReachStatus.REACHABLE, ("1_16",), physical=("1_16",)),
            RoomReach("nook", ReachStatus.REACHABLE, ("1_16",), physical=("1_16",)),
            RoomReach("hall", ReachStatus.REACHABLE, ("1_17",), physical=("1_17",)),
            RoomReach("attic", ReachStatus.AREA_NOT_MAPPED, physical=("1_16",)),
        )
    )
    assert [item.status for item in reach] == [
        ReachStatus.OVERLAP,
        ReachStatus.OVERLAP,
        ReachStatus.REACHABLE,
        ReachStatus.AREA_NOT_MAPPED,
    ]
    assert reach[0].targets == ()
    assert reachable_targets(reach) == {"hall": ("1_17",)}
