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
    await core.rooms.async_revoke_many(("library", "studio"))
    commit = core.state.commit_id

    grant_ids = await core.rooms.async_grant_many(
        (
            GrantRequest("library", ReleaseKind.ONCE),
            GrantRequest("studio", ReleaseKind.TIMED, 4 * 3600),
        )
    )
    rooms = core.state.room_registry.rooms
    assert core.state.commit_id == commit + 1
    assert [rooms["library"].release.grant_id, rooms["studio"].release.grant_id] == (
        list(grant_ids)
    )
    assert rooms["library"].release.kind is ReleaseKind.ONCE
    assert rooms["studio"].release.granted_at == rooms["library"].release.granted_at
    assert rooms["studio"].release.expires_at == NOW + timedelta(hours=4)

    await core.rooms.async_revoke_many(("library", "studio"))
    assert core.state.commit_id == commit + 2
    assert all(room.release is None for room in core.state.room_registry.rooms.values())
    await core.rooms.async_revoke_many(("library",))
    assert core.state.commit_id == commit + 2


@pytest.mark.parametrize(
    ("grants", "error", "code", "path"),
    [
        (
            (
                GrantRequest("library", ReleaseKind.PERMANENT),
                GrantRequest("studio", ReleaseKind.TIMED),
            ),
            ValidationError,
            "release_duration_mismatch",
            ("grants", 1, "duration_seconds"),
        ),
        (
            (
                GrantRequest("library", ReleaseKind.PERMANENT),
                GrantRequest("library", ReleaseKind.ONCE),
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
                GrantRequest("library", ReleaseKind.PERMANENT),
                GrantRequest("studio", ReleaseKind.PERMANENT),
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
    await core.rooms.async_revoke_many(("library",))
    await core.rooms.async_disable("studio")
    before = core.state

    with pytest.raises(error, match=code) as raised:
        await core.rooms.async_grant_many(grants)
    assert raised.value.path == path
    assert core.state is before


async def test_revoking_names_unknown_and_repeated_rooms() -> None:
    core, _adapter, _now = await setup_delay()
    before = core.state
    for rooms, code, path in (
        (("library", "attic"), "unknown_room", ("rooms", 1)),
        (("studio", "studio"), "duplicate_room", ("rooms", 1)),
        ((), "no_rooms", ("rooms",)),
    ):
        with pytest.raises(OrchestratorError, match=code) as raised:
            await core.rooms.async_revoke_many(rooms)
        assert raised.value.path == path
    assert core.state is before
