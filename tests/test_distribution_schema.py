"""Required HA subentry labels and config-entry-only setup contracts."""

import json
from pathlib import Path

import pytest

from custom_components import vacuum_orchestrator

ROOT = Path(__file__).parents[1] / "custom_components/vacuum_orchestrator"


@pytest.mark.parametrize(
    "filename", ["strings.json", "translations/en.json", "translations/de.json"]
)
def test_robot_subentry_has_required_labels_without_obsolete_flow_title(filename):
    strings = json.loads((ROOT / filename).read_text(encoding="utf-8"))
    robot = strings["config_subentries"]["robot"]
    assert isinstance(robot.get("entry_type"), str) and robot["entry_type"].strip()
    assert isinstance(robot.get("initiate_flow", {}).get("user"), str)
    assert robot["initiate_flow"]["user"].strip()
    assert "title" not in robot
    assert robot["step"]["user"]["title"]
    assert robot["step"]["reconfigure"]["title"]


@pytest.mark.parametrize(
    "filename", ["strings.json", "translations/en.json", "translations/de.json"]
)
def test_flows_leave_central_abort_reasons_to_home_assistant(filename):
    strings = json.loads((ROOT / filename).read_text(encoding="utf-8"))
    assert "abort" not in strings["config"]
    assert "abort" not in strings["config_subentries"]["robot"]


def test_yaml_setup_is_reported_without_affecting_other_domains(caplog):
    schema = vacuum_orchestrator.CONFIG_SCHEMA
    assert schema({"other_integration": {}}) == {"other_integration": {}}
    assert not caplog.records
    schema({"vacuum_orchestrator": {}})
    assert "does not support YAML setup" in caplog.text
