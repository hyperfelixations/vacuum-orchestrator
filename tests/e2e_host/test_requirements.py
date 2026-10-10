"""The E2E host's requirements are exactly what Home Assistant pins for it.

HA runs with `--skip-pip`, so every requirement of the integrations it loads
must be installed from `requirements-e2e.txt`; see dev doc "E2E-Host".
"""

import json
import re
from importlib import metadata
from pathlib import Path

import homeassistant.components
from homeassistant import bootstrap

from tests.e2e_host.host import INTEGRATIONS, ROOT, VOI, configuration

CORE = Path(homeassistant.components.__file__).parent
PIN = re.compile(r"([A-Za-z0-9_.-]+)==(\S+)")


def _manifest(domain: str) -> dict:
    custom = {path.name: path for path in (VOI, *INTEGRATIONS.iterdir())}
    path = custom.get(domain, CORE / domain) / "manifest.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _loaded() -> set[str]:
    """Every integration the host sets up, with its dependencies."""
    domains = {
        *bootstrap.CORE_INTEGRATIONS,
        *bootstrap.DEFAULT_INTEGRATIONS,
        *configuration(install_voi=True),
        "http",
        VOI.name,
        *(path.name for path in INTEGRATIONS.iterdir()),
    } - {"homeassistant"}
    pending, found = list(domains), set()
    while pending:
        domain = pending.pop()
        if domain not in found:
            found.add(domain)
            pending += _manifest(domain).get("dependencies", [])
    return found


def _pins() -> dict[str, str]:
    lines = (ROOT / "requirements-e2e.txt").read_text(encoding="utf-8").splitlines()
    return {
        match.group(1).lower(): match.group(0)
        for line in lines
        if (match := PIN.fullmatch(line.strip()))
    }


def test_the_pins_are_the_requirements_of_every_loaded_integration() -> None:
    """Except those the `homeassistant` package installs itself."""
    installed = set(metadata.requires("homeassistant") or [])
    required = {
        requirement.split("==")[0].lower(): requirement
        for domain in _loaded()
        for requirement in _manifest(domain).get("requirements", [])
        if requirement not in installed
    }
    assert _pins() == required


def test_the_frontend_is_pinned_as_its_manifest_pins_it() -> None:
    (frontend,) = _manifest("frontend")["requirements"]
    assert _pins()["home-assistant-frontend"] == frontend


def test_the_simulators_need_no_requirements() -> None:
    for path in INTEGRATIONS.iterdir():
        assert _manifest(path.name)["requirements"] == [], path.name
