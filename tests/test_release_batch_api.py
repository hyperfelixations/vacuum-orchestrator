"""The card releases several rooms at once; errors name the field of the row."""

from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from tests.test_configuration_api import call, configured  # noqa: F401
from tests.test_job_holds_api import Card


def field_of(card: Card) -> str:
    """Return the field path of the card's latest error."""
    return card.connection.translations[-1][3]["field"]


@pytest.mark.usefixtures("configured")
async def test_the_card_releases_rooms_with_their_own_kinds_in_one_commit(
    hass: HomeAssistant,
) -> None:
    card = Card(hass)
    bath = (await call(hass, "create_room", name="Bath"))["room_id"]
    kitchen = (await call(hass, "create_room", name="Kitchen"))["room_id"]
    commit = (await call(hass, "get_rooms"))["commit_id"]

    released = await card.command(
        "release_rooms",
        grants=[
            {"room": bath, "kind": "once"},
            {"room": kitchen, "kind": "timed", "duration_seconds": 4 * 3600},
        ],
    )
    assert released["room_ids"] == [bath, kitchen]
    assert len(released["grant_ids"]) == 2
    assert released["commit_id"] == commit + 1
    rooms = {item["room_id"]: item for item in (await call(hass, "get_rooms"))["rooms"]}
    assert rooms[bath]["release"]["kind"] == "once"
    assert rooms[bath]["release"]["reserved_job_id"] is None
    timed = rooms[kitchen]["release"]
    assert dt_util.parse_datetime(timed["expires_at"]) - dt_util.parse_datetime(
        timed["granted_at"]
    ) == timedelta(hours=4)

    revoked = await card.command("revoke_rooms", rooms=[bath, kitchen])
    assert (revoked["room_ids"], revoked["commit_id"]) == ([bath, kitchen], commit + 2)


@pytest.mark.usefixtures("configured")
async def test_an_invalid_row_changes_nothing_and_names_its_field(
    hass: HomeAssistant,
) -> None:
    card = Card(hass)
    bath = (await call(hass, "create_room", name="Bath"))["room_id"]
    commit = (await call(hass, "get_rooms"))["commit_id"]

    for grants, code, field in (
        (
            [{"room": bath, "kind": "permanent"}, {"room": bath, "kind": "once"}],
            "duplicate_room",
            "grants.1.room",
        ),
        (
            [{"room": bath, "kind": "permanent"}, {"room": bath, "kind": "timed"}],
            "release_duration_mismatch",
            "grants.1.duration_seconds",
        ),
        (
            [{"room": bath, "kind": "forever"}],
            "invalid_parameters",
            "grants.0.kind",
        ),
    ):
        assert await card.command("release_rooms", grants=grants) == {"error": code}
        assert field_of(card) == field
    assert await card.command("revoke_rooms", rooms=[bath, "attic"]) == {
        "error": "unknown_room"
    }
    assert field_of(card) == "rooms.1"
    assert (await call(hass, "get_rooms"))["commit_id"] == commit

    await card.command("release_room", rooms=["attic"], kind="permanent")
    assert field_of(card) == "rooms.0"
