"""Prepare the throwaway E2E instance and drive its simulators.

Exists only in the E2E host's temporary config; see dev doc "E2E-Host". Once
HA started it creates the household's areas and robots, optionally installs
VOI through its config flow, then writes a short-lived session for the owner
to `voi_e2e/session.json` in the config directory, which signals readiness.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import probatio
from homeassistant.auth.const import GROUP_ID_ADMIN
from homeassistant.components import websocket_api
from homeassistant.config_entries import SOURCE_IMPORT, SOURCE_USER
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.typing import ConfigType
from homeassistant.util.file import write_utf8_file

from .simulation import robots

DOMAIN = "voi_e2e_support"
HOUSEHOLD = "voi_e2e/household.json"
SESSION = "voi_e2e/session.json"
ACCESS_TOKEN_LIFETIME = timedelta(minutes=30)
ROBOT_DOMAINS = {"roborock": "roborock", "area": "voi_sim"}
STIMULI = (
    "clean",
    "pause",
    "resume",
    "return_home",
    "dock_after_run",
    "offline",
    "online",
    "set",
    "delay_commands",
    "answer",
)

CONFIG_SCHEMA = probatio.Schema(
    {
        DOMAIN: probatio.Schema(
            {probatio.Optional("install_voi", default=False): cv.boolean}
        )
    },
    extra=probatio.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the commands and prepare the household once HA runs."""
    household = json.loads(
        await hass.async_add_executor_job(
            Path(hass.config.path(HOUSEHOLD)).read_text, "utf-8"
        )
    )
    install_voi: bool = config[DOMAIN]["install_voi"]
    websocket_api.async_register_command(hass, _advance)
    websocket_api.async_register_command(hass, _robot)
    websocket_api.async_register_command(hass, _robots)

    async def prepare(hass: HomeAssistant) -> None:
        await _install(hass, household, install_voi)
        await _write_session(hass)

    async_at_started(hass, prepare)
    return True


async def _install(
    hass: HomeAssistant, household: dict[str, Any], install_voi: bool
) -> None:
    """Create what is missing; a restarted instance keeps what it has."""
    areas = ar.async_get(hass)
    for name in household["areas"]:
        if areas.async_get_area_by_name(name) is None:
            areas.async_create(name)
    for robot in household["robots"]:
        domain = ROBOT_DOMAINS[robot["kind"]]
        if any(
            entry.unique_id == robot["name"]
            for entry in hass.config_entries.async_entries(domain)
        ):
            continue
        result = await hass.config_entries.flow.async_init(
            domain, context={"source": SOURCE_IMPORT}, data=robot
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY, result
    if install_voi and not hass.config_entries.async_entries("vacuum_orchestrator"):
        result = await hass.config_entries.flow.async_init(
            "vacuum_orchestrator", context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["type"] is FlowResultType.CREATE_ENTRY, result
    await hass.async_block_till_done()


async def _write_session(hass: HomeAssistant) -> None:
    """Hand the owner's tokens to the host; the browser refreshes them."""
    users = await hass.auth.async_get_users()
    owner = next((user for user in users if user.is_owner), None)
    if owner is None:
        owner = await hass.auth.async_create_user(
            "Besitzer", group_ids=[GROUP_ID_ADMIN]
        )
    url = f"http://127.0.0.1:{hass.http.server_port}"
    refresh = await hass.auth.async_create_refresh_token(
        owner, f"{url}/", access_token_expiration=ACCESS_TOKEN_LIFETIME
    )
    session = {
        "url": url,
        "client_id": f"{url}/",
        "access_token": hass.auth.async_create_access_token(refresh),
        "refresh_token": refresh.token,
        "expires_in": int(ACCESS_TOKEN_LIFETIME.total_seconds()),
    }
    await hass.async_add_executor_job(
        write_utf8_file, hass.config.path(SESSION), json.dumps(session), True
    )


@websocket_api.websocket_command(
    {
        probatio.Required("type"): "voi_e2e/advance",
        probatio.Required("seconds"): probatio.All(
            probatio.Coerce(float), probatio.Range(min=0)
        ),
    }
)
@websocket_api.require_admin
@callback
def _advance(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Let simulator time pass for every robot."""
    for robot in robots(hass).values():
        robot.advance(msg["seconds"])
    connection.send_result(msg["id"], _describe(hass))


@websocket_api.websocket_command(
    {
        probatio.Required("type"): "voi_e2e/robot",
        probatio.Required("robot"): cv.string,
        probatio.Required("stimulus"): probatio.In(STIMULI),
        probatio.Optional("seconds"): probatio.All(
            probatio.Coerce(float), probatio.Range(min=0)
        ),
        probatio.Optional("key"): cv.string,
        probatio.Optional("value"): cv.string,
    }
)
@websocket_api.require_admin
@callback
def _robot(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Change a robot as a person, its app or its cloud would."""
    robot = robots(hass).get(msg["robot"])
    if robot is None:
        connection.send_error(msg["id"], "not_found", msg["robot"])
        return
    match msg["stimulus"]:
        case "clean":
            robot.clean(msg.get("seconds"))
        case "set":
            try:
                robot.set_role(msg["key"], msg["value"])
            except KeyError as err:
                connection.send_error(msg["id"], "invalid_format", str(err))
                return
        case "offline" | "online":
            robot.set_available(msg["stimulus"] == "online")
        case stimulus:
            getattr(robot, stimulus)()
    connection.send_result(msg["id"], _describe(hass))


@websocket_api.websocket_command({probatio.Required("type"): "voi_e2e/robots"})
@websocket_api.require_admin
@callback
def _robots(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Describe every robot."""
    connection.send_result(msg["id"], _describe(hass))


def _describe(hass: HomeAssistant) -> dict[str, Any]:
    return {name: robot.describe() for name, robot in robots(hass).items()}
