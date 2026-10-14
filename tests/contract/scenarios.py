"""What a card experiences in realistic homes; each scenario becomes a recording.

See dev doc "Aufzeichnungen". A scenario drives the integration only through
the card's WebSocket and changes the home only as its devices would.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vacuum_orchestrator.const import CONF_INSTALLATION_ID, DOMAIN
from tests.contract.recorder import Session, action
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

    async def open(self) -> None:
        """Load the integration, then open the card: subscribe, check and read."""
        await self.load()
        await self.session.request({"type": f"{DOMAIN}/subscribe"})
        await self.session.read_static()
        await self.session.mark("The card opens")

    async def rooms(self) -> dict[str, str]:
        """VOI room IDs by area name, as the card reads them."""
        names = {area_id: name for name, area_id in self.home.areas.items()}
        rooms = (await self.session.collection("get_rooms"))["rooms"]
        return {names[room["area_id"]]: room["room_id"] for room in rooms}

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
    completes the setup. Each command's view event arrives before its result."""
    session = stage.session
    await session.read_static()
    _result(await session.read_manifest())
    await session.mark("Vacuum Orchestrator is installed but not set up")
    await stage.load()
    await session.home("The user adds Vacuum Orchestrator")
    await session.request({"type": f"{DOMAIN}/subscribe"})
    await session.mark("The assistant opens")
    (robot,) = (await session.collection("get_robots"))["robots"]
    await session.command(
        "rename_robot", robot_id=robot["robot_id"], name="Rocky downstairs"
    )
    await session.command("configure_queue", start_delay_seconds=10)
    await session.command("complete_setup")


async def start_delay(stage: Stage) -> None:
    """The queue runs; a job for two locked rooms waits for their release.
    Released one second later, dining room once and studio for four hours, it starts
    after the remaining start delay and cleans both rooms."""
    session, robot = stage.session, stage.home.roborock
    await stage.open()
    await session.action("run_queue")
    await session.action("create_job", areas=stage.areas("Dining Room", "Studio"))
    await session.advance(1)
    rooms = await stage.rooms()
    await session.command(
        "release_rooms",
        grants=[
            {"room": rooms["Dining Room"], "kind": "once"},
            {"room": rooms["Studio"], "kind": "timed", "duration_seconds": 14400},
        ],
    )
    await session.advance(4)
    await session.read()
    robot.clean()
    await session.home("Rocky cleans the dining room and the studio")
    await session.advance(300)
    await session.read()
    robot.return_home()
    await session.home("Rocky drives home")
    await session.advance(60)
    robot.dock_after_run()
    await session.home("Rocky charges at its dock")
    await session.advance(60)
    await session.read()


async def job_hold(stage: Stage) -> None:
    """Editing holds a waiting job: a second card's hold and a start are
    refused with translated errors, saving restarts the start delay. A
    deletion is confirmed under its own hold. Invalid input names its field."""
    session = stage.session
    await stage.open()
    await stage.release("Dining Room", "Studio")
    await session.action("run_queue")
    job = _job_id(await session.action("create_job", areas=stage.areas("Dining Room")))
    edit = await session.command("hold_job", job_id=job, purpose="edit")
    hold = edit["result"]["hold_id"]
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
    confirm = await session.command("hold_job", job_id=job, purpose="confirm")
    await session.action("delete_job", job_id=job, hold_id=confirm["result"]["hold_id"])
    await session.action(
        "create_job", areas=stage.areas("Dining Room"), all_rooms=True, mode="vacuum"
    )
    await session.command("configure_queue", start_delay_seconds=601)


async def reload(stage: Stage) -> None:
    """The integration reloads while the card is open: the card sees it
    unloaded, then loaded with a new runtime and unchanged data."""
    session = stage.session
    await stage.open()
    await session.action("create_job", areas=stage.areas("Studio"))
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
    the robot, then corrects the failed job to completed."""
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
    await session.action("correct_job", job_id=job, outcome="completed")
    await session.read_job(job)
    await session.read()


async def showcase(stage: Stage) -> None:
    """A lived-in home: earlier Rocky finished one job, one was cancelled and
    one never started; two templates are saved and the conservatory is cleaned
    only while its door is open. Then the rooms are released in every way and
    the queue runs: Rocky cleans the studio while a job waits for the locked and
    closed conservatory and two wait for the robot. The editor previews new jobs."""
    session, robot = stage.session, stage.home.roborock
    stage.hass.states.async_set(
        CONSERVATORY_DOOR,
        "off",
        {"device_class": "door", "friendly_name": "Conservatory Door"},
    )
    await stage.open()
    await session.command("complete_setup")
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
    await session.action(
        "create_job",
        areas=stage.areas("Conservatory"),
        mode="mop",
        reason="Guests are coming",
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
    await session.read_history()
    await session.read_diagnostics()
    await _preview_new_jobs(stage)
    await session.mark("A busy afternoon")


async def _preview_new_jobs(stage: Stage) -> None:
    """Preview a new job as the editor drafts it from the defaults: without a
    room yet, for the dining room, for every room and as a dining room mop job."""
    rooms = await stage.rooms()
    draft = {
        "mode": "vacuum",
        "passes": 1,
        "settings_policy": "best_effort",
        "required_on": [],
        "required_off": [],
    }
    await stage.session.preview(**draft)
    await stage.session.preview(areas=[rooms["Dining Room"]], **draft)
    await stage.session.preview(all_rooms=True, **draft)
    await stage.session.preview(
        areas=[rooms["Dining Room"]], **{**draft, "mode": "mop"}
    )


async def queue_end(stage: Stage) -> None:
    """The queue runs without work and waits; a job starts, the user ends the
    queue and it closes once Rocky finished the started job, leaving the next
    job waiting."""
    session, robot = stage.session, stage.home.roborock
    await stage.open()
    await session.command("complete_setup")
    await stage.release("Dining Room", "Studio")
    await session.action("run_queue")
    await session.advance(60)
    await session.mark("The queue waits for work")
    await session.action("create_job", areas=stage.areas("Dining Room"))
    await session.advance(5)
    robot.clean()
    await session.home("Rocky cleans the dining room")
    await session.action("create_job", areas=stage.areas("Studio"))
    await session.action("end_queue")
    await session.mark("The queue ends after the started job")
    await session.advance(300)
    robot.return_home()
    await session.home("Rocky drives home")
    await session.advance(60)
    robot.dock_after_run()
    await session.home("Rocky charges at its dock")
    await session.advance(60)
    await session.mark("The queue has ended")


async def robot_choice(stage: Stage) -> None:
    """Rocky and Dusty both reach the dining room: a waiting job can start on
    either, so the card asks which robot starts it."""
    session = stage.session
    await stage.open()
    await session.command("complete_setup")
    await stage.release("Dining Room", "Studio", "Library", "Conservatory")
    job = _job_id(await session.action("create_job", areas=stage.areas("Dining Room")))
    await session.read_job(job)
    await session.mark("Either robot can start the dining room")


async def no_robot_found(stage: Stage) -> None:
    """The integration is set up in a home without a robot: VOI finds none and
    the card has nothing to clean with."""
    await stage.open()


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


SCENARIOS: dict[str, Scenario] = {
    "not_installed": Scenario(no_robot, not_installed, installed=False, refusals=1),
    "setup": Scenario(single_roborock, setup),
    "showcase": Scenario(single_roborock, showcase),
    "queue_end": Scenario(single_roborock, queue_end),
    "robot_choice": Scenario(two_robots, robot_choice),
    "start_delay": Scenario(single_roborock, start_delay),
    "job_hold": Scenario(single_roborock, job_hold, refusals=4),
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
}


def home_of(home: Household) -> dict[str, Any]:
    """The HA side a consumer needs to render a recording."""
    return {
        "areas": home.areas,
        "vacuums": {name: robot.entity_id for name, robot in home.robots.items()},
    }
