"""What a card experiences in realistic homes; each scenario becomes a recording.

See dev doc "Aufzeichnungen". A scenario drives the integration only through
the card's WebSocket and changes the home only as its devices would. Where a
client sends a form, the scenario checks the form's input right before, with
the same data.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, fields
from typing import Any

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vacuum_orchestrator.const import CONF_INSTALLATION_ID, DOMAIN
from custom_components.vacuum_orchestrator.domain.holds import HOLD_LEASE_SECONDS
from custom_components.vacuum_orchestrator.domain.intents import settings_for_mode
from custom_components.vacuum_orchestrator.domain.job_defaults import JobDefaults
from custom_components.vacuum_orchestrator.domain.types import CleaningMode
from tests.contract.recorder import FINISHED_JOB_STATES, Session, action
from tests.realistic import Household, busy, no_robot, single_roborock, two_robots
from tests.realistic import roborock as roborock_runs


@dataclass
class Stage:
    """The home, the card's connection and the not yet loaded integration."""

    hass: HomeAssistant
    home: Household
    session: Session
    entry: MockConfigEntry

    async def load(self) -> None:
        """Set the integration up as HA does after the user added it."""
        self.entry.add_to_hass(self.hass)
        assert await self.hass.config_entries.async_setup(self.entry.entry_id)
        await self.session.settle()

    async def open(self, *, set_up: bool = True) -> None:
        """Load the integration, then open the card: subscribe, check and read. A
        lived-in home finished the assistant long before."""
        await self.load()
        await self.session.request({"type": f"{DOMAIN}/subscribe"})
        if set_up:
            await self.session.command("complete_setup")
        await self.session.read_static()
        await self.session.mark("The card opens")

    async def rooms(self) -> dict[str, str]:
        """VOI room IDs by area name, as the card reads them; a room without an
        area by its own name."""
        names = {area_id: name for name, area_id in self.home.areas.items()}
        rooms = (await self.session.collection("get_rooms"))["rooms"]
        return {
            names.get(room["area_id"], room["name"]): room["room_id"] for room in rooms
        }

    async def release(self, *names: str) -> None:
        """Release rooms permanently, as the release dialog does."""
        rooms = await self.rooms()
        await self.session.command(
            "release_rooms",
            grants=[{"room": rooms[name], "kind": "permanent"} for name in names],
        )

    def areas(self, *names: str) -> list[str]:
        """HA area IDs by name."""
        return [self.home.areas[name] for name in names]

    async def robot(self, name: str) -> dict[str, Any]:
        """A robot as VOI lists it."""
        robots = (await self.session.collection("get_robots"))["robots"]
        return next(robot for robot in robots if robot["name"] == name)


INITIAL = JobDefaults()


def initial_defaults(**changes: Any) -> dict[str, Any]:
    """Every job default VOI starts with, as `configure_job_defaults` takes them."""
    values = {
        field.name: getattr(INITIAL, field.name)
        for field in fields(INITIAL)
        if field.name != "configured"
    }
    return {
        name: getattr(value, "value", value) for name, value in values.items()
    } | changes


def draft_fields(mode: str = "vacuum", **where: Any) -> dict[str, Any]:
    """A draft before any setting is chosen, so its preview names every level VOI
    offers: the mode, VOI's initial passes and settings policy, no conditions."""
    defaults = initial_defaults()
    return {
        **where,
        "mode": mode,
        "passes": defaults["passes"],
        "settings_policy": defaults["settings_policy"],
        "required_on": [],
        "required_off": [],
    }


def job_fields(
    rooms: list[str] | None = None, mode: str = "vacuum", **changes: Any
) -> dict[str, Any]:
    """A job or template intent naming every setting of its mode, its passes,
    both condition lists and its settings policy, filled from VOI's initial
    defaults; without a room list, for every room."""
    where: dict[str, Any] = (
        {"areas": rooms} if rooms is not None else {"all_rooms": True}
    )
    defaults = initial_defaults()
    settings = sorted(settings_for_mode(CleaningMode(mode)))
    return {
        **where,
        "mode": mode,
        **{name: defaults[name] for name in settings},
        "passes": defaults["passes"],
        "required_on": [],
        "required_off": [],
        "settings_policy": defaults["settings_policy"],
        **changes,
    }


@dataclass(frozen=True)
class Scenario:
    """A named card experience in a household."""

    household: Callable[[HomeAssistant], Household]
    run: Callable[[Stage], Awaitable[None]]
    # Whether HA finds the integration among its custom integrations.
    installed: bool = True
    # Messages the scenario expects to be refused, each with a code.
    refusals: int = 0

    @property
    def description(self) -> str:
        """What the recording shows."""
        return " ".join((self.run.__doc__ or "").split())


# A door sensor of the showcase home; the conservatory is cleaned only while it is open.
CONSERVATORY_DOOR = "binary_sensor.conservatory_door"


def entry() -> MockConfigEntry:
    """The integration's config entry as the config flow creates it."""
    return MockConfigEntry(domain=DOMAIN, data={CONF_INSTALLATION_ID: DOMAIN})


def _job_id(frame: dict[str, Any]) -> str:
    assert frame["success"], frame
    return str(frame["result"]["response"]["job_id"])


def _result(frame: dict[str, Any]) -> Any:
    assert frame["success"], frame
    return frame["result"]


async def not_installed(stage: Stage) -> None:
    """The card on a dashboard before the integration is installed: HA knows
    no such integration."""
    session = stage.session
    await session.read_static()
    assert not (await session.read_manifest())["success"]
    await session.mark("Vacuum Orchestrator is not installed")


async def setup(stage: Stage) -> None:
    """First installation: the card finds the integration installed but not
    set up. Once the user adds it, VOI finds the Roborock and imports the areas
    as locked rooms; the assistant renames the robot, sets the start delay and
    completes the setup, checking each change as the card does before it sends
    one. Each command's view event arrives before its result."""
    session = stage.session
    await session.read_static()
    _result(await session.read_manifest())
    await session.mark("Vacuum Orchestrator is installed but not set up")
    await stage.load()
    await session.home("The user adds Vacuum Orchestrator")
    await session.request({"type": f"{DOMAIN}/subscribe"})
    await session.mark("The assistant opens")
    (robot,) = (await session.collection("get_robots"))["robots"]
    renamed = {"robot_id": robot["robot_id"], "name": "Rocky downstairs"}
    await session.validate("rename_robot", **renamed)
    await session.command("rename_robot", **renamed)
    await session.validate("configure_queue", start_delay_seconds=10)
    await session.command("configure_queue", start_delay_seconds=10)
    await session.command("complete_setup")


async def start_delay(stage: Stage) -> None:
    """The queue runs; a job for two locked rooms waits for their release.
    Released one second later, dining room once and studio for four hours, it starts
    after the remaining start delay and cleans both rooms; halfway through, Rocky
    reports its progress."""
    session, robot = stage.session, stage.home.roborock
    await stage.open()
    await session.action("run_queue")
    await session.action("create_job", areas=stage.areas("Dining Room", "Studio"))
    await session.advance(1)
    await session.mark("The job waits for its two rooms")
    rooms = await stage.rooms()
    grants = [
        {"room": rooms["Dining Room"], "kind": "once"},
        {"room": rooms["Studio"], "kind": "timed", "duration_seconds": 14400},
    ]
    await session.validate("release_rooms", grants=grants)
    await session.command("release_rooms", grants=grants)
    await session.mark("The job starts in four seconds")
    await session.advance(4)
    await session.read()
    robot.clean()
    await session.home("Rocky cleans the dining room and the studio")
    await session.advance(300)
    await session.read()
    robot.set("clean_percent", "50")
    await session.home("Rocky has cleaned half")
    await session.mark("Rocky is halfway through")
    robot.return_home()
    await session.home("Rocky drives home")
    await session.advance(60)
    robot.dock_after_run()
    await session.home("Rocky charges at its dock")
    await session.advance(60)
    await session.read()


async def job_hold(stage: Stage) -> None:
    """Editing holds a waiting job: a second card's hold and a start are
    refused with translated errors, saving restarts the start delay. Invalid
    input names its field. A deletion is confirmed under its own hold; one
    confirmed after its hold lapsed is refused and confirmed again under a
    new hold."""
    session = stage.session
    await stage.open()
    await stage.release("Dining Room", "Studio")
    await session.action("run_queue")
    job = _job_id(await session.action("create_job", areas=stage.areas("Dining Room")))
    edit = await session.command("hold_job", job_id=job, purpose="edit")
    hold = edit["result"]["hold_id"]
    await session.mark("Another card edits the job")
    await session.command("hold_job", job_id=job, purpose="edit")
    await session.action("start_job", job_id=job)
    await session.advance(30)
    await session.command("renew_job_hold", hold_id=hold)
    await session.action(
        "update_job",
        job_id=job,
        hold_id=hold,
        areas=stage.areas("Dining Room", "Studio"),
    )
    await session.action(
        "create_job", areas=stage.areas("Dining Room"), all_rooms=True, mode="vacuum"
    )
    await session.command("configure_queue", start_delay_seconds=601)
    await session.read_job(job)
    await session.mark("The job waits for two rooms")
    lapsed = await session.command("hold_job", job_id=job, purpose="confirm")
    await session.advance(HOLD_LEASE_SECONDS + 1)
    await session.action("delete_job", job_id=job, hold_id=_result(lapsed)["hold_id"])
    confirm = await session.command("hold_job", job_id=job, purpose="confirm")
    await session.action("delete_job", job_id=job, hold_id=_result(confirm)["hold_id"])
    await session.mark("The job for two rooms is gone")


async def edit_hold(stage: Stage) -> None:
    """A card edits a waiting job under its own hold. Leaving the editor gives the
    hold back. A hold the card does not renew, as when its connection drops, lapses:
    its renewal is refused and the card holds the job again."""
    session = stage.session
    await stage.open()
    rooms = await stage.rooms()
    job = _job_id(
        await session.action("create_job", **job_fields([rooms["Dining Room"]]))
    )
    await session.preview(**draft_fields(areas=[rooms["Dining Room"]]))
    await session.mark("A dining room job waits")
    edit = await session.command("hold_job", job_id=job, purpose="edit")
    await session.command("release_job_hold", hold_id=_result(edit)["hold_id"])
    await session.mark("The card gave the job back")
    lapsing = await session.command("hold_job", job_id=job, purpose="edit")
    await session.advance(HOLD_LEASE_SECONDS + 1)
    await session.command("renew_job_hold", hold_id=_result(lapsing)["hold_id"])
    again = await session.command("hold_job", job_id=job, purpose="edit")
    await session.command("release_job_hold", hold_id=_result(again)["hold_id"])
    await session.mark("The card held the job again")


async def reload(stage: Stage) -> None:
    """The integration reloads while the card is open: the card sees it
    unloaded, then loaded with a new runtime and unchanged data."""
    session = stage.session
    await stage.open()
    await session.action("create_job", areas=stage.areas("Studio"))
    await session.mark("A studio job waits")
    assert await stage.hass.config_entries.async_reload(stage.entry.entry_id)
    await session.home("Home Assistant reloads the integration")
    # The card checks the reloaded integration as it does when it opens.
    await session.probe()


async def delayed_answer(stage: Stage) -> None:
    """The robot's cloud answers the start command slowly: the card reads the
    queue while the start is pending, then the start's result arrives."""
    session, robot = stage.session, stage.home.roborock
    await stage.open()
    await stage.release("Library")
    answer = robot.delay_commands()
    async with session.stalled():
        await session.send(
            action("create_job", areas=stage.areas("Library"), start=True)
        )
        await robot.waiting.wait()
        await session.read()
        answer.set()
    await session.home("Rocky's cloud confirms the command")


async def device_fault(stage: Stage) -> None:
    """The mop carriage drops off while Rocky mops and Rocky stops: the job
    waits for the fix and shows when it would fail; reattached, Rocky mops
    again and the job completes."""
    session, robot = stage.session, stage.home.roborock
    await stage.open()
    await stage.release("Dining Room")
    await session.action(
        "create_job", areas=stage.areas("Dining Room"), mode="mop", start=True
    )
    robot.clean()
    await session.home("Rocky mops the dining room")
    await session.advance(60)
    robot.fail("water_carriage_drop")
    await session.home("The mop carriage drops off; Rocky stops")
    await session.advance(300)
    await session.read()
    await session.mark("The job waits for the fix")
    robot.set("vacuum_error", "none")
    robot.pause()
    await session.home("Someone reattaches the mop carriage")
    robot.clean()
    await session.home("Rocky mops again")
    await session.advance(300)
    robot.return_home()
    await session.home("Rocky drives home")
    await session.advance(60)
    robot.dock_after_run()
    await session.home("Rocky charges at its dock")
    await session.advance(60)
    await session.read()


async def mop_run(stage: Stage) -> None:
    """Rocky mops dining room and studio as a Roborock does: the dock washes the mop
    before, between and after the rooms, and its clean water runs out as Rocky
    starts. The job keeps running with the empty tank as attention, completes
    once after the last wash and leaves the attention."""
    session, robot = stage.session, stage.home.roborock
    await stage.open()
    await stage.release("Dining Room", "Studio")
    await session.action(
        "create_job", areas=stage.areas("Dining Room", "Studio"), mode="mop", start=True
    )

    async def reported(changes: dict[str, str]) -> None:
        await session.home(
            "Rocky reports " + ", ".join(f"{k} {v}" for k, v in changes.items())
        )
        # The card at each wash, the empty tank and the return to the dock.
        if "dock_error" in changes or changes.get("status") in {
            "washing_the_mop",
            "charging",
        }:
            await session.read()

    await robot.play(
        roborock_runs.mop_run(2, empty_tank="during"), session.advance, reported
    )
    await session.advance(60)
    await session.read()


async def attention(stage: Stage) -> None:
    """The station reports a full dirty water tank while Rocky rests: VOI asks
    for attention at once; a mop job waits for the robot, a vacuum job starts."""
    session, robot = stage.session, stage.home.roborock
    await stage.open()
    await stage.release("Dining Room", "Studio")
    robot.set("dirty_box_full", "on")
    await session.home("The dirty water tank is full")
    await session.action("run_queue")
    await session.action("create_job", areas=stage.areas("Dining Room"), mode="mop")
    await session.action("create_job", areas=stage.areas("Studio"), mode="vacuum")
    await session.advance(5)
    await session.mark("The station needs attention")


async def external_run(stage: Stage) -> None:
    """Rocky cleans a run started in its app: the card shows it as external and
    a new job waits for the robot; after the run the job starts."""
    session, robot = stage.session, stage.home.roborock
    await stage.open()
    await stage.release("Studio")
    await session.action("run_queue")
    await session.action("create_job", areas=stage.areas("Studio"))
    await session.advance(5)
    await session.read()
    robot.return_home()
    await session.home("Rocky's app run ends; Rocky drives home")
    await session.advance(60)
    robot.dock_after_run()
    await session.home("Rocky charges at its dock")
    await session.advance(5)
    await session.read()


async def recovery(stage: Stage) -> None:
    """Rocky goes offline while cleaning and stays away past the connection
    window: the job needs clarification and keeps its incident. The user frees
    the robot, which ends the job, then corrects it to completed."""
    session, robot = stage.session, stage.home.roborock
    await stage.open()
    await stage.release("Library")
    job = _job_id(
        await session.action("create_job", areas=stage.areas("Library"), start=True)
    )
    robot.clean()
    await session.home("Rocky cleans the library")
    await session.advance(60)
    robot.set_vacuum("unavailable")
    await session.home("Rocky goes offline")
    await session.advance(300)
    await session.read()
    await session.advance(300)
    await session.read()
    await session.read_job(job)
    await session.mark("The job needs clarification")
    robot.dock_after_run()
    await session.home("Rocky is back at its dock")
    (robot_view,) = (await session.collection("get_robots"))["robots"]
    await session.command(
        "resolve_recovery", robot_id=robot_view["robot_id"], confirm_stopped=True
    )
    await session.read_job(job)
    await session.read_history()
    await session.mark("Rocky is free again")
    await session.action("correct_job", job_id=job, outcome="completed")
    await session.read_job(job)
    await session.read()
    await session.read_history()
    await session.mark("The library job is corrected")


async def long_history(stage: Stage) -> None:
    """Rocky vacuumed dining room, studio and library twice each, one job after
    the other; the history shows the six finished jobs five to a page. A card on
    the second page deletes its only job; that page is then past the end and the
    card shows the first page again."""
    session, robot = stage.session, stage.home.roborock
    await stage.open()
    rooms = ("Dining Room", "Studio", "Library")
    await stage.release(*rooms)
    jobs = []
    for turn, name in enumerate(rooms * 2, start=1):
        jobs.append(
            _job_id(
                await session.action("create_job", areas=stage.areas(name), start=True)
            )
        )
        robot.clean()
        await session.home(f"Rocky vacuums the {name.lower()} ({turn})", read=False)
        await session.advance(600)
        robot.return_home()
        await session.home(f"Rocky drives home ({turn})", read=False)
        await session.advance(60)
        robot.dock_after_run()
        await session.home(f"Rocky charges ({turn})", read=False)
        await session.advance(900)
    await session.read_history()
    # A card showing five per page reads its queue so too.
    await session.request({"type": f"{DOMAIN}/queue/get", "offset": 0, "limit": 5})
    for offset in (0, 5):
        await session.request(
            {
                "type": f"{DOMAIN}/jobs/list",
                "offset": offset,
                "limit": 5,
                "states": FINISHED_JOB_STATES,
            }
        )
    # The oldest job, alone on the second page, as its detail page shows it.
    await session.read_job(jobs[0])
    await session.mark("Rocky has cleaned six times")
    await session.action("delete_job", job_id=jobs[0])
    for offset in (5, 0):
        await session.request(
            {
                "type": f"{DOMAIN}/jobs/list",
                "offset": offset,
                "limit": 5,
                "states": FINISHED_JOB_STATES,
            }
        )
    await session.mark("Five jobs are left")


async def showcase(stage: Stage) -> None:
    """A lived-in home: earlier Rocky finished one job, one was cancelled and
    one never started; two templates are saved and the conservatory is cleaned
    only while its door is open. Then the rooms are released in every way and
    the queue runs: Rocky cleans the studio while a job waits for the locked and
    closed conservatory and two wait for the robot. New jobs are previewed."""
    session, robot = stage.session, stage.home.roborock
    stage.hass.states.async_set(
        CONSERVATORY_DOOR,
        "off",
        {"device_class": "door", "friendly_name": "Conservatory Door"},
    )
    await stage.open()
    await session.command(
        "save_template", name="Weekly clean", intent={"all_rooms": True}
    )
    dining_template = _result(
        await session.command(
            "save_template",
            name="Mop the dining room",
            intent={"areas": stage.areas("Dining Room"), "mode": "mop"},
        )
    )["template_id"]
    conservatory = (await stage.rooms())["Conservatory"]
    await session.command(
        "update_room",
        room_id=conservatory,
        configuration={
            "requirements": [
                {"entity_id": CONSERVATORY_DOOR, "accepted_states": ["on"]}
            ]
        },
    )
    # Vacuum due rules: every two days, daily and every three days.
    for name, seconds in (
        ("Dining Room", 172800),
        ("Studio", 86400),
        ("Library", 259200),
    ):
        await session.command(
            "update_room",
            room_id=(await stage.rooms())[name],
            configuration={"due_policy": {"vacuum_seconds": seconds}},
        )
    await stage.release("Dining Room", "Studio", "Library")
    await session.action("create_job", areas=stage.areas("Dining Room"), start=True)
    robot.clean()
    await session.home("Rocky vacuums the dining room")
    await session.advance(600)
    robot.return_home()
    await session.home("Rocky drives home from the dining room")
    await session.advance(60)
    robot.dock_after_run()
    await session.home("Rocky charges after the dining room")
    await session.advance(900)
    studio = _job_id(
        await session.action(
            "create_job", areas=stage.areas("Studio"), mode="mop", start=True
        )
    )
    robot.clean()
    await session.home("Rocky mops the studio")
    await session.advance(120)
    await session.action("cancel_job", job_id=studio, after_cancel="return_to_dock")
    robot.return_home()
    await session.home("Rocky stops and drives home")
    await session.advance(60)
    robot.dock_after_run()
    await session.home("Rocky charges after the cancel")
    await session.advance(900)
    await session.action("create_job", areas=stage.areas("Library"), start=True)
    await session.advance(240)
    await session.advance(3600)
    await session.read_history()
    await session.mark("A quiet afternoon")
    rooms = await stage.rooms()
    await session.command(
        "release_rooms",
        grants=[
            {"room": rooms["Studio"], "kind": "timed", "duration_seconds": 14400},
            {"room": rooms["Library"], "kind": "once"},
        ],
    )
    await session.action("run_queue")
    running = _job_id(await session.action("create_job", areas=stage.areas("Studio")))
    conservatory = _job_id(
        await session.action(
            "create_job",
            areas=stage.areas("Conservatory"),
            mode="mop",
            reason="Guests are coming",
        )
    )
    await session.action(
        "create_job",
        areas=stage.areas("Library"),
        name="Library, thoroughly",
        passes=2,
    )
    await session.command("create_job_from_template", template_id=dining_template)
    await session.advance(5)
    robot.clean()
    await session.home("Rocky cleans the studio")
    await session.advance(240)
    await session.read_job(running)
    await session.read_job(conservatory)
    await session.read_history()
    await session.read_diagnostics()
    await _preview_new_jobs(stage)
    # Five per page: the queue's first page.
    await session.request({"type": f"{DOMAIN}/queue/get", "offset": 0, "limit": 5})
    # A new job saved before a room is chosen: VOI names what is missing.
    await session.validate("create_job", **job_fields([]))
    await session.mark("A busy afternoon")


async def _preview_new_jobs(stage: Stage) -> None:
    """Preview new jobs before any setting is chosen, so VOI names every level
    it offers: without a room yet, also once it waits for the conservatory door,
    for the dining room, for every room and as a dining room mop job."""
    rooms = await stage.rooms()
    await stage.session.preview(**draft_fields())
    await stage.session.preview(**draft_fields() | {"required_on": [CONSERVATORY_DOOR]})
    await stage.session.preview(**draft_fields(areas=[rooms["Dining Room"]]))
    await stage.session.preview(**draft_fields(all_rooms=True))
    await stage.session.preview(**draft_fields("mop", areas=[rooms["Dining Room"]]))


async def queue_end(stage: Stage) -> None:
    """The queue runs without work and waits until its wait time ends; the user
    ends it early, which only confirms."""
    session = stage.session
    await stage.open()
    await stage.release("Dining Room", "Studio")
    await session.action("run_queue")
    await session.advance(60)
    await session.mark("The queue waits for work")
    await session.action("end_queue")
    await session.mark("The queue has ended")


async def robot_choice(stage: Stage) -> None:
    """Rocky and Dusty both reach the dining room once Dusty, an area vacuum
    without modes of its own, vacuums by its profile: a waiting job can start on
    either, and the user starts it on Dusty. While Dusty vacuums, a studio job
    waits that Rocky could start at once."""
    session = stage.session
    await stage.open()
    dusty = await stage.robot("Dusty")
    # A configuration replaces the whole profile; unnamed fields reset.
    profile = {
        "robot_id": dusty["robot_id"],
        "configuration": {**dusty["configuration"], "fixed_mode": "vacuum"},
    }
    await session.validate("configure_robot", **profile)
    await session.command("configure_robot", **profile)
    await stage.release("Dining Room", "Studio", "Library", "Conservatory")
    job = _job_id(await session.action("create_job", areas=stage.areas("Dining Room")))
    await session.read_job(job)
    rooms = await stage.rooms()
    await session.preview(**draft_fields(areas=[rooms["Dining Room"]]))
    await session.mark("Either robot can start the dining room")
    await session.action("start_job", job_id=job, robot_id=dusty["robot_id"])
    stage.home.robots["Dusty"].set_vacuum("cleaning")
    await session.home("Dusty vacuums the dining room")
    await session.mark("Dusty cleans the dining room")
    await session.validate("create_job", **job_fields([rooms["Studio"]]))
    await session.action("create_job", **job_fields([rooms["Studio"]]))
    await session.mark("Rocky could start the studio while Dusty cleans")


async def no_robot_found(stage: Stage) -> None:
    """The integration is set up in a home without a robot: VOI finds none and
    the card has nothing to clean with."""
    await stage.open(set_up=False)


async def unloaded(stage: Stage) -> None:
    """Home Assistant unloads the integration while the card is open, as when
    the user disables it: the card finds it installed but not loaded."""
    await stage.open()
    assert await stage.hass.config_entries.async_unload(stage.entry.entry_id)
    await stage.session.home("Home Assistant unloads the integration", read=False)
    await stage.session.mark("The integration is not loaded")


async def robot_removed(stage: Stage) -> None:
    """The user removed Rocky from VOI: the card offers the vacuum again among
    the candidates and adds it back under its Home Assistant name."""
    session = stage.session
    await stage.open()
    (robot,) = (await session.collection("get_robots"))["robots"]
    await session.command("remove_robot", robot_id=robot["robot_id"])
    await session.mark("Rocky is not configured")
    (candidate,) = (await session.collection("get_robot_candidates"))["candidates"]
    await session.command("add_robot", entity_id=candidate["entity_id"])
    await session.mark("Rocky is configured again")


async def queue_controls(stage: Stage) -> None:
    """Everyday queue work while Rocky cleans the studio: the user moves a job,
    pauses and resumes the queue, adds jobs for one room and for every room,
    edits one, saves it as a template, keeps one in the delete dialog and then
    deletes it under the dialog's hold, starts one from a template
    and ends the queue. Rocky finishes nothing yet: the user cancels its job so
    it stays, sends it home and starts a new job at once. A mark precedes each
    change, so a client can open right before it; before it, the scenario
    previews each new or changed job and reads each job it saves or deletes."""
    session, robot = stage.session, stage.home.roborock
    await stage.open()
    await stage.release("Dining Room", "Studio", "Library")
    rooms = await stage.rooms()
    studio_template = _result(
        await session.command(
            "save_template",
            name="Mop the studio",
            intent=job_fields([rooms["Studio"]], "mop"),
            enabled=True,
            automatic=False,
        )
    )["template_id"]
    await session.action("run_queue")
    studio = _job_id(
        await session.action("create_job", **job_fields([rooms["Studio"]]))
    )
    await session.action("create_job", **job_fields([rooms["Dining Room"]]))
    library = _job_id(
        await session.action("create_job", **job_fields([rooms["Library"]]))
    )
    await session.advance(5)
    robot.clean()
    await session.home("Rocky cleans the studio")
    # A wide card shows the started job's detail beside the list.
    await session.read_job(studio)
    await session.mark("Rocky cleans while two jobs wait")
    await session.action("move_job", job_id=library, direction="up")
    await session.mark("The library job is next")
    await session.action("pause_queue")
    await session.action("resume_queue")
    await session.preview(**draft_fields())
    await session.preview(**draft_fields("mop"))
    await session.preview(**draft_fields("mop", areas=[rooms["Dining Room"]]))
    await session.mark("The queue runs again")
    mop = job_fields([rooms["Dining Room"]], "mop")
    await session.validate("create_job", **mop)
    await session.action("create_job", **mop)
    await session.preview(**draft_fields())
    await session.preview(**draft_fields(all_rooms=True))
    await session.mark("A mop job for the dining room waits")
    await session.validate("create_job", **job_fields())
    everywhere = _job_id(await session.action("create_job", **job_fields()))
    library_draft = draft_fields(areas=[rooms["Library"]])
    await session.preview(**library_draft)
    await session.preview(**library_draft | {"passes": 2})
    await session.mark("A job for every room waits")
    edit = await session.command("hold_job", job_id=library, purpose="edit")
    hold = _result(edit)["hold_id"]
    await session.validate("update_job", job_id=library, hold_id=hold, passes=2)
    await session.action("update_job", job_id=library, hold_id=hold, passes=2)
    await session.read_job(library)
    await session.mark("The library job runs twice")
    named = {"job_id": library, "name": "Library twice", "automatic": False}
    await session.validate("save_job_as_template", **named)
    await session.command("save_job_as_template", **named)
    await session.read_job(everywhere)
    await session.mark("The library job is a template")
    kept = await session.command("hold_job", job_id=everywhere, purpose="confirm")
    await session.command("release_job_hold", hold_id=_result(kept)["hold_id"])
    await session.read_job(everywhere)
    await session.mark("The job for every room stays")
    gone = await session.command("hold_job", job_id=everywhere, purpose="confirm")
    await session.action(
        "delete_job", job_id=everywhere, hold_id=_result(gone)["hold_id"]
    )
    await session.mark("The job for every room is gone")
    await session.command("create_job_from_template", template_id=studio_template)
    await session.mark("The studio template made a job")
    await session.action("end_queue")
    await session.mark("The queue ends after the started job")
    await session.action("cancel_job", job_id=studio, after_cancel="stay")
    robot.stop()
    await session.home("Rocky stops in the studio")
    await session.advance(30)
    await session.mark("Rocky stopped in the studio")
    rocky = await stage.robot("Rocky")
    await session.action("return_robot", robot_id=rocky["robot_id"])
    robot.return_home()
    await session.home("Rocky drives home")
    await session.advance(60)
    robot.dock_after_run()
    await session.home("Rocky charges at its dock")
    await session.advance(60)
    await session.preview(**draft_fields())
    await session.preview(**draft_fields(areas=[rooms["Studio"]]))
    await session.mark("Rocky is back at its dock")
    now = job_fields([rooms["Studio"]], start=True)
    await session.validate("create_job", **now)
    await session.action("create_job", **now)
    await session.mark("Rocky starts the studio at once")


async def room_controls(stage: Stage) -> None:
    """The user arranges the rooms: releases the library for 75 minutes and
    locks the dining room again, excludes and includes the studio, gives the
    dining room its own due rule, adds a room without an area, first without a
    name, and at last locks the studio and the library in one step."""
    session = stage.session
    await stage.open()
    await stage.release("Dining Room")
    rooms = await stage.rooms()
    await session.mark("The dining room is released")
    grants = [{"room": rooms["Library"], "kind": "timed", "duration_seconds": 4500}]
    await session.validate("release_rooms", grants=grants)
    await session.command("release_rooms", grants=grants)
    await session.command("revoke_rooms", rooms=[rooms["Dining Room"]])
    await session.mark("The library is released for 75 minutes")
    await session.command("disable_room", room_id=rooms["Studio"])
    await session.command("enable_room", room_id=rooms["Studio"])
    await session.mark("The studio is included again")
    due = {
        "room_id": rooms["Dining Room"],
        "configuration": {"due_policy": {"vacuum_seconds": 129600}},
    }
    await session.validate("update_room", **due)
    await session.command("update_room", **due)
    await session.mark("The dining room is due every 36 hours")
    await session.validate("create_room", name="")
    await session.validate("create_room", name="Office")
    await session.command("create_room", name="Office")
    await session.mark("The office is a room")
    await stage.release("Studio")
    await session.mark("The studio and the library are released")
    # In the order a card lists them.
    await session.command("revoke_rooms", rooms=[rooms["Studio"], rooms["Library"]])
    await session.mark("Every room is locked")


async def settings_controls(stage: Stage) -> None:
    """The user lets the queue wait 30 minutes for more work, makes mopping with
    the deep route the default for new jobs and lets a job start 30 seconds
    after it could."""
    session = stage.session
    await stage.open()
    await session.validate("configure_queue", grace_seconds=1800)
    await session.command("configure_queue", grace_seconds=1800)
    await session.mark("The queue waits 30 minutes")
    defaults = initial_defaults(mode="mop", mop_route="deep_plus")
    await session.validate("configure_job_defaults", **defaults)
    await session.command("configure_job_defaults", **defaults)
    await session.mark("Mopping is the default")
    await session.validate("configure_queue", start_delay_seconds=30)
    await session.command("configure_queue", start_delay_seconds=30)
    await session.mark("Jobs start 30 seconds after they could")


async def template_controls(stage: Stage) -> None:
    """An automatic template made a job for the due dining room; the user lets it
    make one again, starts one by hand, makes the template manual, adds a studio
    template, first without a name, and removes it again; then it saves a job
    draft as a template. Before saving a template, the scenario previews its
    intent."""
    session = stage.session
    await stage.open()
    await stage.release("Dining Room", "Studio")
    rooms = await stage.rooms()
    await session.command(
        "update_room",
        room_id=rooms["Dining Room"],
        configuration={"due_policy": {"vacuum_seconds": 86400}},
    )
    daily = job_fields([rooms["Dining Room"]])
    template = _result(
        await session.command(
            "save_template",
            name="Dining room daily",
            intent=daily,
            enabled=True,
            automatic=True,
        )
    )["template_id"]
    await session.mark("The daily template made a job")
    await session.command("reset_template_demand", template_id=template)
    await session.mark("The daily template may make a job again")
    await session.command("create_job_from_template", template_id=template)
    await session.preview(**draft_fields(areas=[rooms["Dining Room"]]))
    await session.mark("The daily template made another job")
    # Saving replaces the whole template.
    manual = {
        "template_id": template,
        "name": "Dining room daily",
        "intent": daily,
        "enabled": True,
        "automatic": False,
    }
    await session.validate("save_template", **manual)
    await session.command("save_template", **manual)
    await session.preview(**draft_fields())
    await session.preview(**draft_fields(areas=[rooms["Studio"]]))
    await session.mark("The daily template is manual")
    weekly = {
        "name": "Studio weekly",
        "intent": job_fields([rooms["Studio"]]),
        "enabled": True,
        "automatic": False,
    }
    await session.validate("save_template", **weekly | {"name": None})
    await session.validate("save_template", **weekly)
    studio = _result(await session.command("save_template", **weekly))["template_id"]
    await session.mark("The studio has a template")
    await session.command("remove_template", template_id=studio)
    await session.mark("The studio template is gone")
    # What the job editor shows, not a stored job.
    drafted = {
        "name": "Dining Room",
        "intent": job_fields([rooms["Dining Room"]]),
        "enabled": True,
        "automatic": False,
    }
    await session.validate("save_template", **drafted)
    await session.command("save_template", **drafted)
    await session.mark("The card saved a job draft as a template")


SCENARIOS: dict[str, Scenario] = {
    "not_installed": Scenario(no_robot, not_installed, installed=False, refusals=1),
    "setup": Scenario(single_roborock, setup),
    "showcase": Scenario(single_roborock, showcase),
    "long_history": Scenario(single_roborock, long_history),
    "queue_end": Scenario(single_roborock, queue_end),
    "robot_choice": Scenario(two_robots, robot_choice),
    "start_delay": Scenario(single_roborock, start_delay),
    "job_hold": Scenario(single_roborock, job_hold, refusals=5),
    "edit_hold": Scenario(single_roborock, edit_hold, refusals=1),
    "reload": Scenario(single_roborock, reload),
    "delayed_answer": Scenario(single_roborock, delayed_answer),
    "device_fault": Scenario(single_roborock, device_fault),
    "mop_run": Scenario(single_roborock, mop_run),
    "attention": Scenario(single_roborock, attention),
    "external_run": Scenario(busy, external_run),
    "recovery": Scenario(single_roborock, recovery),
    "no_robot": Scenario(no_robot, no_robot_found),
    "unloaded": Scenario(single_roborock, unloaded, refusals=1),
    "robot_removed": Scenario(single_roborock, robot_removed),
    "queue_controls": Scenario(single_roborock, queue_controls),
    "room_controls": Scenario(single_roborock, room_controls),
    "settings_controls": Scenario(single_roborock, settings_controls),
    "template_controls": Scenario(single_roborock, template_controls),
}


def home_of(home: Household) -> dict[str, Any]:
    """The HA side a consumer needs to render a recording."""
    return {
        "areas": home.areas,
        "vacuums": {name: robot.entity_id for name, robot in home.robots.items()},
    }
