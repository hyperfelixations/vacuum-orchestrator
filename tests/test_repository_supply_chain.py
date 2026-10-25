"""Supply-chain contract of the repository files.

The files are read as text; whether an advisory exists is Dependabot's verdict.
"""

import json
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
INTEGRATION = ROOT / "custom_components" / "vacuum_orchestrator"

# Vendors publish no releases for these actions, so no commit could be followed.
HACS_ACTION = "hacs/action@main"
HASSFEST_ACTION = "home-assistant/actions/hassfest@master"

# pytest-homeassistant-custom-component pins this set, which moves only with the
# supported Home Assistant baseline.
HOME_ASSISTANT_STACK = {
    "homeassistant",
    "pytest",
    "pytest-asyncio",
    "pytest-cov",
    "pytest-homeassistant-custom-component",
}
# Core's Roborock requirements at the baseline (tests/adapters/test_roborock_core.py).
CORE_ROBOROCK = {"python-roborock", "vacuum-map-parser-roborock"}
SEMVER_TYPES = {
    "version-update:semver-major",
    "version-update:semver-minor",
    "version-update:semver-patch",
}
SCHEDULE = {"interval": "cron", "cronjob": "0 5 1,15 * *"}
PATCH_RULE = {"dependency-name": "*", "update-types": ["version-update:semver-patch"]}
VERSION_GROUP = {
    "patterns": ["*"],
    "update-types": ["minor", "patch"],
    "applies-to": "version-updates",
}
SECURITY_GROUP = {"patterns": ["*"], "applies-to": "security-updates"}
PINNED_USES = re.compile(r"uses: [\w.-]+/[\w./-]+@[0-9a-f]{40} # v\d+(?:\.\d+){0,2}$")
EXACT_PIN = re.compile(r"([A-Za-z0-9_.-]+)==\d[\w.]*")


def load(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def trigger_config(workflow: dict[str, Any]) -> dict[str, Any]:
    # YAML 1.1 reads the key `on` as the boolean True.
    return workflow[True] if True in workflow else workflow["on"]


def triggers(workflow: dict[str, Any]) -> set[str]:
    return set(trigger_config(workflow))


def steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return list(job.get("steps", []))


def workflow_names() -> list[str]:
    return sorted(path.name for path in WORKFLOWS.glob("*.y*ml"))


WORKFLOW_NAMES = ["ci.yml", "release.yml", "validate.yml"]


def test_exactly_the_ci_release_and_validate_workflows_exist() -> None:
    assert workflow_names() == WORKFLOW_NAMES


@pytest.mark.parametrize("name", WORKFLOW_NAMES)
def test_every_action_is_pinned_to_a_commit_sha_with_its_version(name: str) -> None:
    for line in (WORKFLOWS / name).read_text(encoding="utf-8").splitlines():
        if not re.match(r"\s*(?:- )?uses:", line):
            continue
        if line.split("uses:", 1)[1].strip() in {HACS_ACTION, HASSFEST_ACTION}:
            continue
        assert PINNED_USES.search(line.strip()), f"{name}: {line.strip()}"


def test_floating_actions_get_the_least_permissions_they_need() -> None:
    for name in workflow_names():
        workflow = load(WORKFLOWS / name)
        for job_name, job in workflow["jobs"].items():
            permissions = job.get("permissions", workflow.get("permissions"))
            for step in steps(job):
                where = f"{name}:{job_name}"
                if step.get("uses") == HACS_ACTION:
                    assert permissions == {}, f"{where} needs permissions: {{}}"
                if step.get("uses") == HASSFEST_ACTION:
                    assert permissions == {"contents": "read"}, where


@pytest.mark.parametrize("name", WORKFLOW_NAMES)
def test_workflows_read_by_default_and_keep_no_checkout_credentials(
    name: str,
) -> None:
    workflow = load(WORKFLOWS / name)
    assert workflow["permissions"] in ({}, {"contents": "read"})
    for job_name, job in workflow["jobs"].items():
        assert isinstance(job.get("timeout-minutes"), int), f"{name}:{job_name}"
        for step in steps(job):
            if str(step.get("uses", "")).startswith("actions/checkout@"):
                assert step["with"]["persist-credentials"] is False, job_name


def test_ci_runs_on_push_and_pull_request() -> None:
    workflow = load(WORKFLOWS / "ci.yml")
    assert triggers(workflow) == {"push", "pull_request", "workflow_dispatch"}


def test_ci_runs_pushes_to_main_and_pull_requests_once() -> None:
    configured = trigger_config(load(WORKFLOWS / "ci.yml"))
    assert configured["push"] == {"branches": ["main"]}
    assert configured["pull_request"] is None


def test_ci_runs_the_e2e_host_with_its_requirements() -> None:
    job = load(WORKFLOWS / "ci.yml")["jobs"]["e2e-host"]
    commands = [step["run"] for step in steps(job) if "run" in step]
    assert "python -m pip install -r requirements-e2e.txt" in commands
    assert "python -m pytest -m e2e_host --no-cov tests/e2e_host" in commands


def test_validate_has_no_pull_request_trigger_and_no_default_permissions() -> None:
    workflow = load(WORKFLOWS / "validate.yml")
    assert triggers(workflow) == {"push", "schedule", "workflow_dispatch"}
    assert workflow["permissions"] == {}


def test_validate_runs_hassfest_and_the_unmodified_hacs_action() -> None:
    jobs = load(WORKFLOWS / "validate.yml")["jobs"]
    all_steps = [step for job in jobs.values() for step in steps(job)]
    used = {step["uses"] for step in all_steps if "uses" in step}
    assert {HACS_ACTION, HASSFEST_ACTION} <= used
    hacs = next(step for step in all_steps if step.get("uses") == HACS_ACTION)
    assert hacs["with"] == {"category": "integration"}


def dependabot_updates() -> dict[str, dict[str, Any]]:
    updates = load(ROOT / ".github" / "dependabot.yml")["updates"]
    return {entry["package-ecosystem"]: entry for entry in updates}


def test_dependabot_watches_pip_and_the_actions_in_the_repository_root() -> None:
    updates = dependabot_updates()
    assert set(updates) == {"pip", "github-actions"}
    for entry in updates.values():
        assert entry["directory"] == "/"
    # Dependabot reads the watched directory only; a manifest elsewhere stays stale.
    for folder in ("custom_components", "tests"):
        assert not list((ROOT / folder).rglob("requirements*.txt")), folder
        assert not list((ROOT / folder).rglob("pyproject.toml")), folder


def test_version_updates_run_on_the_1st_and_15th_after_a_week_without_patches() -> None:
    for ecosystem, entry in dependabot_updates().items():
        assert entry["schedule"] == SCHEDULE, ecosystem
        assert entry["cooldown"] == {"default-days": 7}, ecosystem
        assert entry["ignore"][0] == PATCH_RULE, ecosystem


def test_minor_updates_are_grouped_and_security_updates_come_grouped() -> None:
    updates = dependabot_updates()
    pip, actions = updates["pip"], updates["github-actions"]
    assert pip["groups"] == {"tooling": VERSION_GROUP, "security": SECURITY_GROUP}
    assert actions["groups"] == {"actions": VERSION_GROUP, "security": SECURITY_GROUP}
    assert actions["ignore"] == [PATCH_RULE]


def test_no_ignore_rule_can_hold_back_security_updates() -> None:
    """A rule that names only a package would block its security updates as well."""
    for ecosystem, entry in dependabot_updates().items():
        for rule in entry["ignore"]:
            assert rule.get("update-types") and "versions" not in rule, ecosystem


def pinned(name: str) -> set[str]:
    """The exactly pinned packages of a requirements file."""
    text = (ROOT / name).read_text(encoding="utf-8")
    names = set()
    for line in (line.strip() for line in text.splitlines()):
        if not line or line.startswith("#") or line == "-r requirements-test.txt":
            continue
        match = EXACT_PIN.fullmatch(line)
        assert match, f"{name}: {line} is not an exact pin"
        names.add(match.group(1))
    return names


def test_dependabot_leaves_the_home_assistant_test_stack_to_the_baseline() -> None:
    """Core's Roborock pins and the E2E pins are HA's own and move with it as well."""
    rules = dependabot_updates()["pip"]["ignore"][1:]
    ignored = {rule["dependency-name"]: rule for rule in rules}
    assert set(ignored) == (
        HOME_ASSISTANT_STACK | CORE_ROBOROCK | pinned("requirements-e2e.txt")
    )
    # A rule without update-types would also block the package's security updates.
    for name, rule in ignored.items():
        assert set(rule["update-types"]) == SEMVER_TYPES, name


def test_every_test_requirement_is_pinned_exactly() -> None:
    assert pinned("requirements-test.txt") >= HOME_ASSISTANT_STACK | CORE_ROBOROCK
    lines = (ROOT / "requirements-e2e.txt").read_text(encoding="utf-8").splitlines()
    assert "-r requirements-test.txt" in lines
    assert pinned("requirements-e2e.txt")


def test_the_integration_has_no_third_party_runtime_requirements() -> None:
    """Adding one needs a pip-audit gate for the runtime closure first."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    manifest = json.loads((INTEGRATION / "manifest.json").read_text(encoding="utf-8"))
    assert project["project"]["dependencies"] == []
    assert manifest["requirements"] == []
