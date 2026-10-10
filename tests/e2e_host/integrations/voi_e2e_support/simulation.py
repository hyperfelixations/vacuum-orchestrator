"""The run program both simulators share; see dev doc "E2E-Host".

Commands start a run; simulator time (`voi_e2e/advance`) ends it and drives
the robot home. Stimuli change the robot as a person or its app would.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from enum import StrEnum
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er

DATA_ROBOTS = "voi_e2e_robots"
CLEAN_SECONDS_PER_TARGET = 120
RETURN_SECONDS = 30


class Activity(StrEnum):
    """The vacuum activity a simulator reports."""

    CLEANING = "cleaning"
    DOCKED = "docked"
    ERROR = "error"
    IDLE = "idle"
    PAUSED = "paused"
    RETURNING = "returning"


class SimulatedRobot:
    """One simulated robot: activity, timed run program and recorded commands."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.activity = Activity.DOCKED
        self.available = True
        self.commands: list[dict[str, Any]] = []
        # Simulator seconds until the current run or drive home ends.
        self.remaining: float | None = None
        # Whether a delayed command waits for `answer`.
        self.pending = False
        self._listeners: list[Callable[[], None]] = []
        self._answer: asyncio.Event | None = None

    def listen(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Call the listener after every change; return its removal."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def changed(self) -> None:
        """Let the entities write their new state."""
        for listener in list(self._listeners):
            listener()

    async def command(self, name: str, params: Any = None) -> None:
        """Record a command, wait for a delayed answer, then carry it out."""
        self.commands.append({"command": name, "params": params})
        if self._answer is not None:
            self.pending = True
            try:
                await self._answer.wait()
            finally:
                self._answer, self.pending = None, False
        self.execute(name, params)

    def execute(self, name: str, params: Any) -> None:
        """Carry out a command; subclasses know their vendor's commands."""
        raise NotImplementedError

    def delay_commands(self) -> None:
        """Answer the next command only after `answer`."""
        self._answer = asyncio.Event()

    def answer(self) -> None:
        """Let a delayed command complete."""
        if self._answer is not None:
            self._answer.set()

    def advance(self, seconds: float) -> None:
        """Let simulator time pass: a run ends and the robot drives home."""
        while seconds > 0 and self.remaining is not None:
            if self.activity not in (Activity.CLEANING, Activity.RETURNING):
                return
            step = min(seconds, self.remaining)
            seconds -= step
            self.remaining -= step
            if self.remaining > 0:
                continue
            if self.activity is Activity.CLEANING:
                self.return_home()
            else:
                self.dock_after_run()

    def clean(self, seconds: float | None = None) -> None:
        """Clean; a run without duration lasts until a stimulus ends it."""
        self.activity = Activity.CLEANING
        self.remaining = seconds
        self.changed()

    def pause(self) -> None:
        """Interrupt the run where the robot is; the run time stays."""
        self.activity = Activity.PAUSED
        self.changed()

    def resume(self) -> None:
        """Continue a paused run."""
        self.activity = Activity.CLEANING
        self.changed()

    def stop(self) -> None:
        """End the run where the robot is."""
        self.activity = Activity.IDLE
        self.remaining = None
        self.changed()

    def return_home(self) -> None:
        """Drive home."""
        self.activity = Activity.RETURNING
        self.remaining = RETURN_SECONDS
        self.changed()

    def dock_after_run(self) -> None:
        """Rest at the dock."""
        self.activity = Activity.DOCKED
        self.remaining = None
        self.changed()

    def set_available(self, available: bool) -> None:
        """Lose or regain the connection to the robot."""
        self.available = available
        self.changed()

    def set_role(self, key: str, value: str) -> None:
        """Set a companion entity; only robots with roles have them."""
        raise KeyError(key)

    def describe(self) -> dict[str, Any]:
        """What tests may assert on."""
        return {
            "activity": self.activity.value,
            "available": self.available,
            "remaining_seconds": self.remaining,
            "pending_command": self.pending,
            "commands": self.commands,
        }


def robots(hass: HomeAssistant) -> dict[str, SimulatedRobot]:
    """The simulated robots by name."""
    found: dict[str, SimulatedRobot] = hass.data.setdefault(DATA_ROBOTS, {})
    return found


def map_areas(
    hass: HomeAssistant, entity_id: str, mapping: dict[str, list[str]]
) -> None:
    """Map HA areas by name to segments once, as a user does in HA's dialog."""
    registry = er.async_get(hass)
    entry = registry.async_get(entity_id)
    if entry is None or "area_mapping" in entry.options.get("vacuum", {}):
        return
    areas = ar.async_get(hass)
    found = {name: areas.async_get_area_by_name(name) for name in mapping}
    registry.async_update_entity_options(
        entity_id,
        "vacuum",
        {
            "area_mapping": {
                area.id: segments
                for name, segments in mapping.items()
                if (area := found[name]) is not None
            }
        },
    )
