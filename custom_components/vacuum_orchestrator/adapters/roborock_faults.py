"""Scope of every Roborock `vacuum_error` and `dock_error` option.

The tables cover exactly the options of the installed core integration; a test
compares them. Unknown codes are general faults. See dev doc "Gerätefehler".
"""

from ..domain.faults import FaultScope

GENERAL = FaultScope.GENERAL
VACUUM = FaultScope.VACUUM
MOP = FaultScope.MOP
STATION = FaultScope.STATION
STATION_VACUUM = FaultScope.STATION_VACUUM
STATION_MOP = FaultScope.STATION_MOP
NOTICE = FaultScope.NOTICE

ROBOT_NEUTRAL = frozenset({"none"})
DOCK_NEUTRAL = frozenset({"ok"})

ROBOT_FAULTS: dict[str, FaultScope] = {
    "audio_error": NOTICE,
    "battery_error": GENERAL,
    "bumper_stuck": GENERAL,
    "cannot_cross_carpet": GENERAL,
    "charging_error": GENERAL,
    "check_clean_carouse": STATION_MOP,
    "clean_carousel_exception": STATION_MOP,
    "clean_carousel_water_full": STATION_MOP,
    "clear_brush_exception": STATION_MOP,
    "clear_brush_exception_2": GENERAL,
    "clear_water_box_exception": STATION_MOP,
    "clear_water_box_hoare": STATION_MOP,
    "cliff_sensor_error": GENERAL,
    "collect_dust_error_3": STATION_VACUUM,
    "collect_dust_error_4": STATION_VACUUM,
    "compass_error": GENERAL,
    "dirty_water_box_hoare": STATION_MOP,
    "dock": STATION,
    "dock_locator_error": GENERAL,
    "drain_water_exception": STATION_MOP,
    "fan_error": VACUUM,
    "filter_blocked": VACUUM,
    "filter_screen_exception": STATION_MOP,
    "internal_error": GENERAL,
    "invisible_wall_detected": GENERAL,
    "lidar_blocked": GENERAL,
    "light_touch": GENERAL,
    "low_battery": GENERAL,
    "main_brush_jammed": VACUUM,
    "mopping_roller_1": STATION_MOP,
    "mopping_roller_error_2": STATION_MOP,
    "no_dustbin": VACUUM,
    "nogo_zone_detected": GENERAL,
    "optical_flow_sensor_dirt": GENERAL,
    "return_to_dock_fail": GENERAL,
    "robot_on_carpet": GENERAL,
    "robot_tilted": GENERAL,
    "robot_trapped": GENERAL,
    "side_brush_error": VACUUM,
    "side_brush_jammed": VACUUM,
    "sink_strainer_hoare": STATION_MOP,
    "strainer_error": VACUUM,
    "temperature_protection": GENERAL,
    "up_water_exception": STATION_MOP,
    "vertical_bumper_pressed": GENERAL,
    "vibrarise_jammed": MOP,
    "visual_sensor": GENERAL,
    "wall_sensor_dirty": GENERAL,
    "water_carriage_drop": MOP,
    "wheels_jammed": GENERAL,
    "wheels_suspended": GENERAL,
}

DOCK_FAULTS: dict[str, FaultScope] = {
    "cleaning_tank_full_or_blocked": STATION_MOP,
    "dirty_tank_latch_open": STATION_MOP,
    "duct_blockage": STATION_VACUUM,
    "no_dustbin": STATION_VACUUM,
    "waste_water_tank_full": STATION_MOP,
    "water_empty": STATION_MOP,
}

# Roborock status values that are faults themselves; `device_offline` is not.
STATUS_FAULTS = frozenset({"error", "charging_problem", "locked"})
