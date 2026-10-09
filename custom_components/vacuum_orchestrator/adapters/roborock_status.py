"""Phase of every Roborock `status` option.

The table covers exactly the options of the installed core integration; a test
compares them. Unknown values are `unknown`. See dev doc "Gerätezustand".
"""

from ..domain.types import RobotPhase

CLEANING = RobotPhase.CLEANING
RETURNING = RobotPhase.RETURNING
STATION = RobotPhase.STATION
DOCKED = RobotPhase.DOCKED
IDLE = RobotPhase.IDLE
PAUSED = RobotPhase.PAUSED
ERROR = RobotPhase.ERROR
OFFLINE = RobotPhase.OFFLINE
OTHER = RobotPhase.OTHER
UNKNOWN = RobotPhase.UNKNOWN

STATUS_PHASES: dict[str, RobotPhase] = {
    "air_drying_stopping": STATION,
    "attaching_the_mop": STATION,
    "charger_disconnected": IDLE,
    "charging": DOCKED,
    "charging_complete": DOCKED,
    "charging_problem": ERROR,
    "cleaning": CLEANING,
    "detaching_the_mop": STATION,
    "device_offline": OFFLINE,
    "docking": RETURNING,
    "egg_attack": OTHER,
    "emptying_the_bin": STATION,
    "error": ERROR,
    "going_to_target": OTHER,
    "going_to_wash_the_mop": RETURNING,
    "idle": IDLE,
    "locked": ERROR,
    "manual_mode": OTHER,
    "mapping": OTHER,
    "mopping": CLEANING,
    "paused": PAUSED,
    "relocating": OTHER,
    "remote_control_active": OTHER,
    "returning_home": RETURNING,
    "saving_map": OTHER,
    "segment_cleaning": CLEANING,
    "shutting_down": OTHER,
    "sleeping": IDLE,
    "spot_cleaning": CLEANING,
    "starting": OTHER,
    "sweep_and_mop": CLEANING,
    "sweeping": CLEANING,
    "transitioning": OTHER,
    "unknown": UNKNOWN,
    "updating": OTHER,
    "waiting_to_charge": STATION,
    "washing_the_mop": STATION,
    "zoned_cleaning": CLEANING,
}

AT_DOCK = frozenset(
    status for status, phase in STATUS_PHASES.items() if phase in {DOCKED, STATION}
) | {"charging_problem"}
