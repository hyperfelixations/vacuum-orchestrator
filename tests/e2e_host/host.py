"""A throwaway Home Assistant with the real frontend, VOI, simulators and the card.

See dev doc "E2E-Host". `build` writes the config directory, `Host` runs HA
from it on 127.0.0.1 and stops it through `homeassistant.stop`.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import asdict
from pathlib import Path
from typing import Any

import yaml
from homeassistant.components import onboarding
from homeassistant.components.http import config as http_config
from homeassistant.components.lovelace import dashboard, resources

from tests.realistic import AREAS, generic, roborock

ROOT = Path(__file__).parents[2]
HERE = Path(__file__).parent
INTEGRATIONS = HERE / "integrations"
VOI = ROOT / "custom_components" / "vacuum_orchestrator"
CARD = "vacuum-orchestrator-card.js"
CARD_TYPE = "custom:vacuum-orchestrator-card"
SESSION = Path("voi_e2e") / "session.json"
HOUSEHOLD = Path("voi_e2e") / "household.json"
LOG = "host.log"
STARTUP_SECONDS = 180
STOP_SECONDS = 60
# Runs with more sections play the longest script.
MAX_SECTIONS = 6


def roborock_robot(name: str, *, cleaning: bool = False) -> dict[str, Any]:
    """A Roborock V1 with dock as `tests.realistic.roborock` registers it."""
    return {
        "kind": "roborock",
        "name": name,
        "duid": roborock.duid_of(name),
        "model": roborock.MODEL,
        "mac": roborock.MAC,
        "features": int(roborock.FEATURES),
        "fan_speeds": roborock.FAN_SPEEDS,
        "roles": [
            {**asdict(role), "category": role.category.value} for role in roborock.ROLES
        ],
        "maps": [
            {"flag": flag, "name": name, "rooms": rooms}
            for flag, name, rooms in roborock.MAPS
        ],
        "mapping": roborock.MAPPING,
        "cleaning": cleaning,
        # Scripted runs by cleaning kind and number of sections.
        "runs": {
            "vacuum": [roborock.vacuum_run(n) for n in range(1, MAX_SECTIONS + 1)],
            "mop": [roborock.mop_run(n) for n in range(1, MAX_SECTIONS + 1)],
        },
        "placeholders": {"now": roborock.NOW, "run_start": roborock.RUN_START},
    }


def area_robot(name: str) -> dict[str, Any]:
    """A vacuum of another integration as `tests.realistic.generic` registers it."""
    return {
        "kind": "area",
        "name": name,
        "features": int(generic.FEATURES),
        "mapping": generic.MAPPING,
    }


# The households of `tests.realistic`; `test_build.py` keeps them equal.
HOUSEHOLDS = {
    "no_robot": (),
    "single_roborock": (roborock_robot("Saugi"),),
    "generic_area": (area_robot("Flitzi"),),
    "two_robots": (roborock_robot("Saugi"), area_robot("Flitzi")),
    "busy": (roborock_robot("Saugi", cleaning=True),),
}


def configuration(*, install_voi: bool) -> dict[str, Any]:
    """`configuration.yaml`: synthetic home, no recorder; HTTP see `_http`."""
    return {
        "homeassistant": {
            "name": "VOI E2E",
            "latitude": 0,
            "longitude": 0,
            "elevation": 0,
            "unit_system": "metric",
            "currency": "EUR",
            "country": "DE",
            "language": "de",
            "time_zone": "Europe/Berlin",
        },
        "logger": {"default": "warning"},
        "voi_e2e_support": {"install_voi": install_voi},
    }


def build(
    config: Path,
    *,
    port: int,
    household: str = "single_roborock",
    install_voi: bool = False,
    card: Path | None = None,
) -> None:
    """Write a config directory that starts onboarded, with dashboard and card."""
    _write_json(
        config / HOUSEHOLD,
        {"areas": list(AREAS), "robots": list(HOUSEHOLDS[household])},
    )
    (config / "configuration.yaml").write_text(
        yaml.safe_dump(configuration(install_voi=install_voi), sort_keys=False),
        encoding="utf-8",
    )
    _http(config, port)
    _store(
        config,
        onboarding.STORAGE_KEY,
        onboarding.STORAGE_VERSION,
        {"done": list(onboarding.STEPS)},
    )
    components = config / "custom_components"
    for source in (VOI, *sorted(INTEGRATIONS.iterdir())):
        shutil.copytree(
            source,
            components / source.name,
            ignore=shutil.ignore_patterns("__pycache__"),
            dirs_exist_ok=True,
        )
    _write_json(components / "roborock" / "translations" / "en.json", _roborock_names())
    if card is not None:
        _install_card(config, card)


def _http(config: Path, port: int) -> None:
    """Loopback as the confirmed HTTP config.

    HA 2026.10 tries `http:` from YAML only as a pending config and reverts it
    to the default (all interfaces, port 8123) unless a user confirms it.
    """
    stable = {
        **http_config.HTTP_STORAGE_SCHEMA(
            {"server_host": ["127.0.0.1"], "server_port": port}
        ),
        http_config.HTTP_CONFIG_CREATED_AT: "2026-01-01T00:00:00+00:00",
        http_config.HTTP_CONFIG_ERROR: None,
        http_config.HTTP_CONFIG_ERROR_MESSAGE: None,
    }
    _store(
        config,
        http_config.STORAGE_KEY,
        http_config.STORAGE_VERSION,
        {
            http_config.KEY_STABLE: stable,
            http_config.KEY_PENDING: None,
            http_config.KEY_YAML_MIGRATION_DONE: True,
        },
        minor_version=http_config.STORAGE_MINOR_VERSION,
    )


def _install_card(config: Path, card: Path) -> None:
    """Serve the card from `www/` and show it on the default dashboard."""
    target = config / "www" / CARD
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(card, target)
    version = hashlib.sha256(target.read_bytes()).hexdigest()[:12]
    _store(
        config,
        resources.RESOURCE_STORAGE_KEY,
        resources.RESOURCES_STORAGE_VERSION,
        {
            "items": [
                {"id": "voc", "type": "module", "url": f"/local/{CARD}?v={version}"}
            ]
        },
    )
    _store(
        config,
        dashboard.CONFIG_STORAGE_KEY_DEFAULT,
        dashboard.CONFIG_STORAGE_VERSION,
        {
            "config": {
                "views": [
                    {
                        "title": "Staubsauger",
                        "path": "voi",
                        "type": "sections",
                        "max_columns": 4,
                        "sections": [
                            {
                                "type": "grid",
                                "cards": [
                                    {
                                        "type": CARD_TYPE,
                                        "grid_options": {"columns": "full"},
                                    }
                                ],
                            }
                        ],
                    }
                ]
            }
        },
    )


def _roborock_names() -> dict[str, Any]:
    """Entity names by translation key, as core's `strings.json` has them."""
    names: dict[str, dict[str, dict[str, str]]] = {}
    for role in roborock.ROLES:
        if role.translation_key is not None:
            names.setdefault(role.domain, {})[role.translation_key] = {
                "name": role.name
            }
    return {"entity": names}


def _store(
    config: Path,
    key: str,
    version: int,
    data: dict[str, Any],
    *,
    minor_version: int = 1,
) -> None:
    _write_json(
        config / ".storage" / key,
        {"version": version, "minor_version": minor_version, "key": key, "data": data},
    )


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def free_port() -> int:
    """A port on 127.0.0.1 that is free right now."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class Host:
    """One HA process on a built config directory."""

    def __init__(self, config: Path) -> None:
        self.config = config
        self.process: subprocess.Popen[bytes] | None = None
        self.session: dict[str, Any] = {}

    @property
    def url(self) -> str:
        """Where HA serves the frontend."""
        return str(self.session["url"])

    def start(self, timeout: float = STARTUP_SECONDS) -> dict[str, Any]:
        """Start HA and wait until `voi_e2e_support` wrote the session."""
        session = self.config / SESSION
        session.unlink(missing_ok=True)
        with (self.config / LOG).open("ab") as log:
            self.process = subprocess.Popen(
                [sys.executable, str(HERE / "launch.py"), str(self.config)],
                cwd=self.config,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        deadline = time.monotonic() + timeout
        while not session.exists():
            if self.process.poll() is not None or time.monotonic() > deadline:
                self.kill()
                raise RuntimeError(f"Home Assistant did not start:\n{self.log_tail()}")
            time.sleep(0.25)
        self.session = json.loads(session.read_text(encoding="utf-8"))
        return self.session

    def stop(self, timeout: float = STOP_SECONDS) -> None:
        """Stop HA as an administrator would; kill it if it hangs."""
        if self.process is None or self.process.poll() is not None:
            return
        request = urllib.request.Request(
            f"{self.url}/api/services/homeassistant/stop",
            data=b"{}",
            headers={
                "Authorization": f"Bearer {self.session['access_token']}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        # HA may close the connection while it stops.
        with suppress(OSError), urllib.request.urlopen(request, timeout=10):
            pass
        try:
            self.process.wait(timeout)
        except subprocess.TimeoutExpired:
            self.kill()
            raise

    def kill(self) -> None:
        """End the process at once."""
        if self.process is not None and self.process.poll() is None:
            self.process.kill()
            self.process.wait()

    def log_tail(self, lines: int = 40) -> str:
        """The end of HA's console output."""
        path = self.config / LOG
        if not path.exists():
            return ""
        text = path.read_text(encoding="utf-8", errors="replace")
        return "\n".join(text.splitlines()[-lines:])


@contextmanager
def running(
    *,
    household: str = "single_roborock",
    install_voi: bool = False,
    card: Path | None = None,
    config: Path | None = None,
) -> Iterator[Host]:
    """Build, start and finally stop a host; a temporary config is removed."""
    directory = config or Path(tempfile.mkdtemp(prefix="voi-e2e-"))
    try:
        build(
            directory,
            port=free_port(),
            household=household,
            install_voi=install_voi,
            card=card,
        )
        host = Host(directory)
        host.start()
        try:
            yield host
        finally:
            host.stop()
    finally:
        if config is None:
            shutil.rmtree(directory, ignore_errors=True)
