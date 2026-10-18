"""Every scenario records what a card exchanges with the real integration.

See dev doc "Aufzeichnungen". Nothing is committed: a consumer such as the
card records them on demand with `python -m tests.contract --record-to <dir>`.
"""

import json
import re
import subprocess
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.translation import (
    _async_get_translations_cache as translation_cache,
)
from homeassistant.helpers.translation import (
    _TranslationsCacheData as TranslationsCacheData,
)
from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from custom_components.vacuum_orchestrator.api.configuration import VALIDATED_COMMANDS
from custom_components.vacuum_orchestrator.const import API_VERSION, DOMAIN
from tests.contract.__main__ import arguments
from tests.contract.recorder import (
    FINISHED_JOB_STATES,
    FORMAT,
    ROOT,
    Session,
    deterministic,
    dump,
    provenance,
    serve_frontend_messages,
    voi_commit,
    voi_dirty,
)
from tests.contract.scenarios import (
    CONSERVATORY_DOOR,
    SCENARIOS,
    Stage,
    entry,
    home_of,
)
from tests.test_runtime import MemoryBackend

# Recorded twice per run: same code, same bytes.
REPEATED = ("mop_run", "showcase")
# A recording in this run, by scenario, for the repeat.
RECORDED: dict[str, str] = {}
# What a recording must never contain: machine paths, mail and network addresses.
PRIVATE = re.compile(
    r"[A-Za-z]:\\\\|/(?:home|Users|root|tmp)/|[\w.+-]+@[\w-]+\.[\w.]+"
    r"|\b(?!127\.0\.0\.1\b)(?:\d{1,3}\.){3}\d{1,3}\b"
)


@pytest.fixture
async def record(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    monkeypatch: pytest.MonkeyPatch,
    freezer: FrozenDateTimeFactory,
    request: pytest.FixtureRequest,
) -> Any:
    """Record one scenario in this test's Home Assistant."""

    async def run(name: str) -> dict[str, Any]:
        deterministic(monkeypatch, freezer)
        MemoryBackend.data = None
        monkeypatch.setattr(
            "custom_components.vacuum_orchestrator.runtime.HomeAssistantSnapshotBackend",
            MemoryBackend,
        )
        scenario = SCENARIOS[name]
        if scenario.installed:
            request.getfixturevalue("enable_custom_integrations")
        else:
            # HA's test plugin shares loaded translations between tests; a home
            # without the integration has none of its texts.
            translation_cache(hass).cache_data = TranslationsCacheData({}, {})
        home = scenario.household(hass)
        await serve_frontend_messages(hass)
        session = await Session.connect(hass, hass_ws_client, freezer)
        await scenario.run(Stage(hass, home, session, entry()))
        await session.settle()
        return {
            "provenance": provenance(voi_commit(), dirty=voi_dirty()),
            "scenario": name,
            "description": scenario.description,
            "home": home_of(home),
            "hass": session.initial_hass,
            "steps": session.steps,
        }

    return run


def check(name: str, recording: dict[str, Any]) -> None:
    """A recording a consumer can rely on."""
    assert recording["provenance"]["format"] == FORMAT
    assert recording["provenance"]["api_version"] == API_VERSION
    sent = {
        step["send"]["id"]: step["send"]
        for step in recording["steps"]
        if "send" in step
    }
    results = [
        step["receive"]
        for step in recording["steps"]
        if step.get("receive", {}).get("type") == "result"
    ]
    assert sorted(result["id"] for result in results) == sorted(sent), (
        f"{name}: every message has exactly one result"
    )
    refused = [result for result in results if not result["success"]]
    assert len(refused) == SCENARIOS[name].refusals, f"{name}: {refused}"
    for result in refused:
        error, message = result["error"], sent[result["id"]]
        assert error["code"], error
        assert error["message"], error
        if DOMAIN in (message["type"].split("/")[0], message.get("domain")):
            assert error["translation_domain"] == DOMAIN, error
            assert error["translation_key"], error
    leaked = PRIVATE.search(dump(recording))
    assert leaked is None, f"{name} contains {leaked.group()!r}"


@pytest.mark.parametrize("name", sorted(SCENARIOS))
async def test_scenario_records(
    record: Any, request: pytest.FixtureRequest, name: str
) -> None:
    recording = await record(name)
    check(name, recording)
    text = RECORDED.setdefault(name, dump(recording))
    target = request.config.getoption("--record-to")
    if target is not None:
        target.mkdir(parents=True, exist_ok=True)
        (target / f"{name}.json").write_text(text, encoding="utf-8", newline="\n")


def results(recording: dict[str, Any], kind: str) -> list[Any]:
    """The results of every message of one type, without the domain prefix."""
    asked = {
        step["send"]["id"]
        for step in recording["steps"]
        if step.get("send", {}).get("type") == f"{DOMAIN}/{kind}"
    }
    return [
        step["receive"]["result"]
        for step in recording["steps"]
        if step.get("receive", {}).get("id") in asked
    ]


def answers(recording: dict[str, Any], query: str) -> list[Any]:
    """The results of every configuration query of one kind."""
    asked = {
        step["send"]["id"]
        for step in recording["steps"]
        if step.get("send", {}).get("query") == query
    }
    return [
        step["receive"]["result"]
        for step in recording["steps"]
        if step.get("receive", {}).get("id") in asked
    ]


async def test_the_showcase_records_what_the_editor_and_the_map_show(
    record: Any,
) -> None:
    recording = await record("showcase")
    maps = {
        entity_id: state["a"]["entity_picture"]
        for entity_id, state in recording["hass"]["states"].items()
        if entity_id.startswith("image.")
    }
    assert sorted(maps) == ["image.rocky_lower_floor", "image.rocky_main_floor"]
    assert all(picture.startswith("/api/image_proxy/") for picture in maps.values())
    assert len(answers(recording, "preview_job")) == 5
    conservatory = next(
        room
        for result in answers(recording, "get_rooms")[-1:]
        for room in result["rooms"]
        if room["name"] == "Conservatory"
    )
    assert [item["entity_id"] for item in conservatory["requirements"]] == [
        CONSERVATORY_DOOR
    ]


async def test_a_full_queue_holds_seven_open_jobs(record: Any) -> None:
    """After the busy afternoon three more jobs wait: one cleans, six wait."""
    recording = await record("showcase")
    end = recording["steps"].index({"mark": "Seven jobs are open"})
    sent = {
        step["send"]["id"]: step["send"]
        for step in recording["steps"][:end]
        if "send" in step
    }
    queue = [
        step["receive"]["result"]
        for step in recording["steps"][:end]
        if "receive" in step
        and sent.get(step["receive"]["id"], {}).get("type") == f"{DOMAIN}/queue/get"
    ][-1]
    assert [
        queue["total"],
        [job["state"] for job in queue["jobs"]].count("queued"),
    ] == [
        6,
        6,
    ]


async def test_the_showcase_rooms_are_fresh_due_and_without_a_rule(
    record: Any,
) -> None:
    (*_, rooms) = answers(await record("showcase"), "get_rooms")
    assert {
        room["name"]: room["due"]["vacuum"]["state"] for room in rooms["rooms"]
    } == {
        "Dining Room": "fresh",
        "Studio": "due",
        "Library": "due",
        "Conservatory": "disabled",
    }


async def test_either_robot_can_start_the_waiting_job(record: Any) -> None:
    recording = await record("robot_choice")
    steps = recording["steps"]
    either = steps.index({"mark": "Either robot can start the dining room"})
    (*_, queue) = [
        result
        for result in results({"steps": steps[:either]}, "queue/get")
        if result["limit"] > 1 and result["jobs"]
    ]
    (job,) = queue["jobs"]
    candidates = job["waiting"]["robots"]["candidates"]
    assert len(candidates) == 2
    assert all(not candidate["blockers"] for candidate in candidates)
    (preview,) = answers(recording, "preview_job")
    options = preview["settings"]["vacuum_power"]["options"]
    assert options
    assert not any(option["supported_by_all"] for option in options)


async def test_rocky_could_start_a_job_while_dusty_cleans(record: Any) -> None:
    recording = await record("robot_choice")
    (*_, queue) = [
        result for result in results(recording, "queue/get") if result["limit"] > 1
    ]
    (studio,) = queue["jobs"]
    assert studio["actions"]["start"]["available"]
    (*_, running) = [
        result for result in results(recording, "jobs/list") if result["jobs"]
    ]
    assert [job["state"] for job in running["jobs"]] == ["running"]


async def test_the_showcase_reads_the_job_waiting_for_the_conservatory(
    record: Any,
) -> None:
    waiting = [
        job
        for job in results(await record("showcase"), "job/get")
        if job["state"] == "queued"
    ]
    assert [job["waiting"]["code"] for job in waiting] == ["room_not_released"]


async def test_the_corrected_job_can_be_retried_and_corrected_again(
    record: Any,
) -> None:
    recording = await record("recovery")
    (*_, corrected) = results(recording, "job/get")
    assert corrected["state"] == "completed"
    assert all(corrected["actions"][name]["available"] for name in ("retry", "correct"))
    assert {"mark": "The library job is corrected"} in recording["steps"]


async def test_a_freed_robot_leaves_its_job_finished_and_correctable(
    record: Any,
) -> None:
    *_, freed, _corrected = results(await record("recovery"), "job/get")
    assert freed["state"] in {"completed", "failed", "cancelled"}
    assert freed["actions"]["correct"]["available"]


async def test_the_history_shows_six_finished_jobs_five_to_a_page(
    record: Any,
) -> None:
    # The two pages before a card deletes a job; what follows has its own test.
    pages = [
        result
        for result in results(await record("long_history"), "jobs/list")
        if result["limit"] == 5
    ][:2]
    assert [len(page["jobs"]) for page in pages] == [5, 1]
    assert {job["state"] for page in pages for job in page["jobs"]} == {"completed"}


async def test_the_docked_robot_could_start_the_studio_at_once(record: Any) -> None:
    (*_, studio) = answers(await record("queue_controls"), "preview_job")
    assert studio["startable_now"] is True


# Homes that are not set up yet; every other home finished the assistant long ago.
FRESH = ("not_installed", "setup", "no_robot")


@pytest.mark.parametrize("name", sorted(set(SCENARIOS) - set(FRESH)))
async def test_a_lived_in_home_finished_the_assistant(record: Any, name: str) -> None:
    (first, *_) = answers(await record(name), "get_setup")
    assert first["assistant_pending"] is False


async def test_a_card_checks_the_integration_again_after_a_reload(
    record: Any,
) -> None:
    steps = (await record("reload"))["steps"]
    reloaded = steps.index({"home": "Home Assistant reloads the integration"})
    assert {"type": f"{DOMAIN}/queue/get", "offset": 0, "limit": 1} in [
        {key: value for key, value in step["send"].items() if key != "id"}
        for step in steps[reloaded:]
        if "send" in step
    ]


# Where the card's tests open a card, by scenario.
MARKS = {
    "attention": "The station needs attention",
    "recovery": "The job needs clarification",
    "setup": "The assistant opens",
    "no_robot": "The card opens",
    "unloaded": "The integration is not loaded",
    "robot_removed": "Rocky is not configured",
}


def after_mark(recording: dict[str, Any], mark: str) -> list[dict[str, Any]]:
    """The steps after a mark, up to the next change the card did not read."""
    steps = recording["steps"]
    start = steps.index({"mark": mark}) + 1
    end = next(
        (index for index in range(start, len(steps)) if "home" in steps[index]),
        len(steps),
    )
    return steps[start:end]


# The changes of a scenario in order: the first ones after each mark where a
# client opens right before them.
CONTROLS: dict[str, list[tuple[str, list[str]]]] = {
    "queue_controls": [
        ("Rocky cleans while two jobs wait", ["move_job"]),
        ("The library job is next", ["pause_queue", "resume_queue"]),
        ("The queue runs again", ["create_job"]),
        ("A mop job for the dining room waits", ["create_job"]),
        ("A job for every room waits", ["hold_job", "update_job"]),
        ("The library job runs twice", ["save_job_as_template"]),
        ("The library job is a template", ["hold_job", "release_job_hold"]),
        ("The job for every room stays", ["hold_job", "delete_job"]),
        ("The job for every room is gone", ["create_job_from_template"]),
        ("The studio template made a job", ["end_queue"]),
        ("The queue ends after the started job", ["cancel_job"]),
        ("Rocky stopped in the studio", ["return_robot"]),
        ("Rocky is back at its dock", ["create_job"]),
    ],
    "queue_end": [
        ("The queue waits for work", ["end_queue"]),
    ],
    "edit_hold": [
        ("A dining room job waits", ["hold_job", "release_job_hold"]),
        (
            "The card gave the job back",
            ["hold_job", "renew_job_hold", "hold_job", "release_job_hold"],
        ),
    ],
    "job_hold": [
        (
            "The job waits for two rooms",
            ["hold_job", "delete_job", "hold_job", "delete_job"],
        ),
    ],
    "room_controls": [
        ("The dining room is released", ["release_rooms", "revoke_rooms"]),
        ("The library is released for 75 minutes", ["disable_room", "enable_room"]),
        ("The studio is included again", ["update_room"]),
        ("The dining room is due every 36 hours", ["create_room"]),
        ("The studio and the library are released", ["revoke_rooms"]),
    ],
    "settings_controls": [
        ("The card opens", ["configure_queue"]),
        ("The queue waits 30 minutes", ["configure_job_defaults"]),
        ("Mopping is the default", ["configure_queue"]),
    ],
    "template_controls": [
        ("The daily template made a job", ["reset_template_demand"]),
        ("The daily template may make a job again", ["create_job_from_template"]),
        ("The daily template made another job", ["save_template"]),
        ("The daily template is manual", ["save_template"]),
        ("The studio has a template", ["remove_template"]),
        ("The studio template is gone", ["save_template"]),
    ],
    "robot_choice": [
        ("The card opens", ["configure_robot"]),
        ("Either robot can start the dining room", ["start_job"]),
        ("Dusty cleans the dining room", ["create_job"]),
    ],
    "robot_removed": [
        ("Rocky is not configured", ["add_robot"]),
    ],
    "long_history": [
        ("Rocky has cleaned six times", ["delete_job"]),
    ],
}


def commands_after(recording: dict[str, Any], mark: str) -> list[str]:
    """The changes sent after a mark, up to the next mark or home change."""
    steps = recording["steps"]
    start = steps.index({"mark": mark}) + 1
    sent: list[str] = []
    for step in steps[start:]:
        if "mark" in step or "home" in step:
            break
        message = step.get("send", {})
        if message.get("type") == "call_service":
            sent.append(message["service"])
        elif message.get("type") == f"{DOMAIN}/configuration/command":
            sent.append(message["command"])
    return sent


@pytest.mark.parametrize("name", sorted(CONTROLS))
async def test_each_change_follows_its_mark(record: Any, name: str) -> None:
    recording = await record(name)
    for mark, commands in CONTROLS[name]:
        assert commands_after(recording, mark)[: len(commands)] == commands, mark


@pytest.mark.parametrize("name", sorted(CONTROLS))
async def test_a_mark_follows_the_last_change(record: Any, name: str) -> None:
    steps = (await record(name))["steps"]
    changes = [
        index
        for index, step in enumerate(steps)
        if step.get("send", {}).get("type")
        in ("call_service", f"{DOMAIN}/configuration/command")
    ]
    assert any("mark" in step for step in steps[changes[-1] :])


# Commands a client sends from a form; adding a robot from the list asks nothing.
FORMS = frozenset(VALIDATED_COMMANDS) - {"add_robot"}
CHANGES = ("call_service", f"{DOMAIN}/configuration/command")


def first_change_after(
    recording: dict[str, Any], mark: str
) -> tuple[str, Any, dict[str, Any], Any]:
    """The first change after a mark with its data, and the message sent right
    before it with that message's answer."""
    steps = recording["steps"]
    sent = [
        step["send"] for step in steps[steps.index({"mark": mark}) :] if "send" in step
    ]
    before, change = next(pair for pair in pairwise(sent) if pair[1]["type"] in CHANGES)
    (answer,) = [
        step["receive"].get("result")
        for step in steps
        if step.get("receive", {}).get("id") == before["id"]
    ]
    if change["type"] == "call_service":
        return change["service"], change["service_data"], before, answer
    return change["command"], change["parameters"], before, answer


@pytest.mark.parametrize("name", sorted(CONTROLS))
async def test_a_client_checks_each_form_right_before_sending_it(
    record: Any, name: str
) -> None:
    recording = await record(name)
    for mark, _ in CONTROLS[name]:
        command, data, before, verdict = first_change_after(recording, mark)
        if command not in FORMS:
            continue
        assert before.get("query") == "validate", mark
        assert before["parameters"] == {"command": command, "data": data}, mark
        assert verdict["valid"] is True, mark


def verdicts(recording: dict[str, Any], command: str) -> list[list[list[str]]]:
    """The errors of every recorded check of one command, as path and code."""
    asked = {
        step["send"]["id"]
        for step in recording["steps"]
        if step.get("send", {}).get("query") == "validate"
        and step["send"]["parameters"]["command"] == command
    }
    return [
        [
            [".".join(map(str, error["path"])), error["code"]]
            for error in step["receive"]["result"]["errors"]
        ]
        for step in recording["steps"]
        if step.get("receive", {}).get("id") in asked
    ]


# The errors a client shows at a form's fields, by scenario: the command and
# the errors of each of its checks in order.
FORM_ERRORS = {
    "showcase": ("create_job", [[["areas", "job_requires_area"]]]),
    "room_controls": ("create_room", [[["name", "invalid_room_name"]], []]),
    "template_controls": ("save_template", [[], [["name", "invalid_value"]], [], []]),
}


@pytest.mark.parametrize("name", sorted(FORM_ERRORS))
async def test_the_form_errors_a_client_shows_are_recorded(
    record: Any, name: str
) -> None:
    command, errors = FORM_ERRORS[name]
    assert verdicts(await record(name), command) == errors


@pytest.mark.parametrize("name", sorted(MARKS))
async def test_a_card_state_has_its_mark(record: Any, name: str) -> None:
    recording = await record(name)
    assert {"mark": MARKS[name]} in recording["steps"]


async def test_an_unloaded_integration_is_only_checked(record: Any) -> None:
    steps = after_mark(await record("unloaded"), MARKS["unloaded"])
    sent = [step["send"] for step in steps if "send" in step]
    assert [{k: v for k, v in message.items() if k != "id"} for message in sent] == [
        {"type": f"{DOMAIN}/queue/get", "offset": 0, "limit": 1}
    ]
    (refused,) = [step["receive"] for step in steps if "receive" in step]
    assert refused["error"]["code"] == "orchestrator_not_loaded"


async def test_deleting_the_last_job_of_a_page_leaves_that_page_past_the_end(
    record: Any,
) -> None:
    """A card on the history's second page deletes its only job: that page is
    then empty and past the end, and the card reads the first page again."""
    steps = after_mark(await record("long_history"), "Rocky has cleaned six times")
    sent = {step["send"]["id"]: step["send"] for step in steps if "send" in step}
    pages = [
        (sent[step["receive"]["id"]]["offset"], step["receive"]["result"])
        for step in steps
        if "receive" in step
        and sent.get(step["receive"]["id"], {}).get("type") == f"{DOMAIN}/jobs/list"
        and sent[step["receive"]["id"]].get("states") == FINISHED_JOB_STATES
    ]
    assert [(offset, len(page["jobs"]), page["total"]) for offset, page in pages] == [
        (5, 0, 5),
        (0, 5, 5),
    ]


async def test_a_deletion_under_a_lapsed_hold_is_confirmed_again(record: Any) -> None:
    steps = (await record("job_hold"))["steps"]
    start = steps.index({"mark": "The job waits for two rooms"})
    deletions = {
        step["send"]["id"]
        for step in steps[start:]
        if step.get("send", {}).get("service") == "delete_job"
    }
    answers = [
        step["receive"].get("error", {}).get("translation_key")
        for step in steps[start:]
        if step.get("receive", {}).get("id") in deletions
    ]
    assert answers == ["hold_expired", None]


async def test_a_removed_robot_is_offered_again(record: Any) -> None:
    recording = await record("robot_removed")
    (candidates, *_) = answers(recording, "get_robot_candidates")[1:]
    assert [item["name"] for item in candidates["candidates"]] == ["Rocky"]
    assert answers(recording, "get_robots")[-1]["robots"][0]["name"] == "Rocky"


@pytest.mark.parametrize("name", REPEATED)
async def test_a_scenario_records_the_same_bytes_again(record: Any, name: str) -> None:
    first = RECORDED.get(name)
    if first is None:
        pytest.skip(f"runs after test_scenario_records[{name}] in the same run")
    again = json.loads(dump(await record(name)))
    for index, (old, new) in enumerate(
        zip(json.loads(first)["steps"], again["steps"], strict=False)
    ):
        assert old == new, f"{name} differs at step {index}"
    assert dump(again) == first, name


def test_recordings_are_not_committed() -> None:
    tracked = subprocess.run(
        ["git", "ls-files", "tests/contract"],
        cwd=ROOT,
        capture_output=True,
        check=True,
        text=True,
    ).stdout.split()
    assert not [path for path in tracked if path.endswith(".json")]


def test_the_entry_point_records_every_scenario_into_its_directory(
    tmp_path: Path,
) -> None:
    command = arguments(tmp_path / "out")
    assert command[command.index("--record-to") + 1] == str(tmp_path / "out")
    assert command[command.index("-k") + 1] == "test_scenario_records"
    assert "--no-cov" in command
