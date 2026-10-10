"""A simulated Roborock V1 robot with dock; see dev doc "E2E-Host".

A commanded run plays a script of `tests.realistic.roborock` in simulator
time; stimuli change the robot as `RoborockV1`'s helpers do.
"""

from __future__ import annotations

from typing import Any

from custom_components.voi_e2e_support.simulation import Activity, SimulatedRobot
from homeassistant.util import dt as dt_util
from homeassistant.util import slugify


class SimulatedRoborock(SimulatedRobot):
    """Companion roles, maps and the V1 commands VOI and core send."""

    def __init__(self, spec: dict[str, Any]) -> None:
        super().__init__(spec["name"])
        self.spec = spec
        self.slug = slugify(spec["duid"])
        self.roles = {role["key"]: role["state"] for role in spec["roles"]}
        self.fan_speed = "balanced"
        # Steps of the commanded run still to come and when it began.
        self.script: list[tuple[float, dict[str, str]]] = []
        self._run_start = ""

    @property
    def current_map(self) -> int | None:
        """The flag of the selected map."""
        selected = self.roles.get("selected_map")
        flags = [item["flag"] for item in self.spec["maps"] if item["name"] == selected]
        return flags[0] if len(flags) == 1 else None

    def segments(self) -> list[tuple[str, str, str]]:
        """`(<flag>_<room>, room name, map name)` of every map."""
        return [
            (f"{item['flag']}_{room}", name, item["name"])
            for item in self.spec["maps"]
            for room, name in item["rooms"].items()
        ]

    def execute(self, name: str, params: Any) -> None:
        """Carry out a V1 command as the robot would."""
        match name:
            case "app_segment_clean":
                (request,) = params
                self.run(len(request["segments"]) * request.get("repeat", 1))
            case "app_start" if self.activity is Activity.PAUSED:
                self.resume()
            case "app_start":
                rooms = [
                    item["rooms"]
                    for item in self.spec["maps"]
                    if item["flag"] == self.current_map
                ]
                self.run(len(rooms[0]) if rooms else 1)
            case "app_pause":
                self.pause()
            case "app_stop":
                self.stop()
            case "app_charge":
                self.return_home()
            case "set_custom_mode":
                (self.fan_speed,) = params
                self.changed()

    def run(self, sections: int) -> None:
        """Start the scripted run of the selected cleaning mode."""
        kind = "vacuum" if self.roles.get("cleaning_mode") == "vacuum" else "mop"
        scripts = self.spec["runs"][kind]
        self.script = list(map(tuple, scripts[min(sections, len(scripts)) - 1]))
        self._run_start = dt_util.utcnow().isoformat()
        self._play(0)

    def advance(self, seconds: float) -> None:
        """Play the scripted run, or let a stimulated run end."""
        if not self.script:
            super().advance(seconds)
            return
        if self.activity is not Activity.PAUSED:
            self._play(seconds)

    def _play(self, seconds: float) -> None:
        while self.script and self.script[0][0] <= seconds:
            delay, changes = self.script.pop(0)
            seconds -= delay
            times = {
                self.spec["placeholders"]["now"]: dt_util.utcnow().isoformat(),
                self.spec["placeholders"]["run_start"]: self._run_start,
            }
            for key, reported in changes.items():
                value = times.get(reported, reported)
                if key == "vacuum":
                    self.activity = Activity(value)
                else:
                    self.roles[key] = value
            self.changed()
        if self.script:
            delay, changes = self.script[0]
            self.script[0] = (delay - seconds, changes)

    def clean(self, seconds: float | None = None) -> None:
        """Clean, as the robot reports any run (`RoborockV1.clean`)."""
        self.roles.update(status="segment_cleaning", in_cleaning="on")
        super().clean(seconds)

    def pause(self) -> None:
        """Pause and keep the run."""
        self.roles.update(status="paused")
        super().pause()

    def resume(self) -> None:
        """Continue the paused run."""
        self.roles.update(status="segment_cleaning")
        super().resume()

    def stop(self) -> None:
        """Stop where the robot is; a scripted run ends."""
        self.script = []
        self.roles.update(status="idle", in_cleaning="off")
        super().stop()

    def return_home(self) -> None:
        """Drive home after a run; a scripted run ends."""
        self.script = []
        self.roles.update(status="returning_home", in_cleaning="off")
        super().return_home()

    def dock_after_run(self) -> None:
        """Rest at the dock and charge."""
        self.script = []
        self.roles.update(status="charging", in_cleaning="off")
        super().dock_after_run()

    def set_role(self, key: str, value: str) -> None:
        """Set a companion entity as the robot or its dock reports it."""
        if key not in self.roles:
            raise KeyError(key)
        self.roles[key] = value
        self.changed()

    def describe(self) -> dict[str, Any]:
        """Add roles, fan speed and the steps still to come."""
        return {
            **super().describe(),
            "roles": self.roles,
            "fan_speed": self.fan_speed,
            "script_steps": len(self.script),
        }
