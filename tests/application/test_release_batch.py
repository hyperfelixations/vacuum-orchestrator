"""Several rooms are released or revoked in one commit, or not at all."""

from datetime import timedelta

import pytest

from custom_components.vacuum_orchestrator.domain.errors import (
    ConflictError,
    OrchestratorError,
    ValidationError,
)
from custom_components.vacuum_orchestrator.domain.releases import (
    GrantRequest,
    ReleaseKind,
)
from tests.application.test_orchestrator import NOW
from tests.application.test_start_delay import setup_delay


async def test_mixed_grants_share_one_commit_and_one_instant() -> None:
    core, _adapter, _now = await setup_delay()
    await core.rooms.async_revoke_many(("kitchen", "hall"))
    commit = core.state.commit_id

    grant_ids = await core.rooms.async_grant_many(
        (
            GrantRequest("kitchen", ReleaseKind.ONCE),
            GrantRequest("hall", ReleaseKind.TIMED, 4 * 3600),
        )
    )
    rooms = core.state.room_registry.rooms
    assert core.state.commit_id == commit + 1
    assert [rooms["kitchen"].release.grant_id, rooms["hall"].release.grant_id] == (
        list(grant_ids)
    )
    assert rooms["kitchen"].release.kind is ReleaseKind.ONCE
    assert rooms["hall"].release.granted_at == rooms["kitchen"].release.granted_at
    assert rooms["hall"].release.expires_at == NOW + timedelta(hours=4)

    await core.rooms.async_revoke_many(("kitchen", "hall"))
    assert core.state.commit_id == commit + 2
    assert all(room.release is None for room in core.state.room_registry.rooms.values())
    await core.rooms.async_revoke_many(("kitchen",))
    assert core.state.commit_id == commit + 2


@pytest.mark.parametrize(
    ("grants", "error", "code", "path"),
    [
        (
            (
                GrantRequest("kitchen", ReleaseKind.PERMANENT),
                GrantRequest("hall", ReleaseKind.TIMED),
            ),
            ValidationError,
            "release_duration_mismatch",
            ("grants", 1, "duration_seconds"),
        ),
        (
            (
                GrantRequest("kitchen", ReleaseKind.PERMANENT),
                GrantRequest("kitchen", ReleaseKind.ONCE),
            ),
            ValidationError,
            "duplicate_room",
            ("grants", 1, "room"),
        ),
        (
            (GrantRequest("attic", ReleaseKind.PERMANENT),),
            ValidationError,
            "unknown_room",
            ("grants", 0, "room"),
        ),
        (
            (
                GrantRequest("kitchen", ReleaseKind.PERMANENT),
                GrantRequest("hall", ReleaseKind.PERMANENT),
            ),
            ConflictError,
            "room_unavailable",
            ("grants", 1, "room"),
        ),
        ((), ValidationError, "no_rooms", ("grants",)),
    ],
)
async def test_one_invalid_grant_changes_nothing_and_names_its_field(
    grants, error, code, path
) -> None:
    core, _adapter, _now = await setup_delay()
    await core.rooms.async_revoke_many(("kitchen",))
    await core.rooms.async_disable("hall")
    before = core.state

    with pytest.raises(error, match=code) as raised:
        await core.rooms.async_grant_many(grants)
    assert raised.value.path == path
    assert core.state is before


async def test_revoking_names_unknown_and_repeated_rooms() -> None:
    core, _adapter, _now = await setup_delay()
    before = core.state
    for rooms, code, path in (
        (("kitchen", "attic"), "unknown_room", ("rooms", 1)),
        (("hall", "hall"), "duplicate_room", ("rooms", 1)),
        ((), "no_rooms", ("rooms",)),
    ):
        with pytest.raises(OrchestratorError, match=code) as raised:
            await core.rooms.async_revoke_many(rooms)
        assert raised.value.path == path
    assert core.state is before
