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
SEMVER_TYPES = {
    "version-update:semver-major",
    "version-update:semver-minor",
    "version-update:semver-patch",
}
PINNED_USES = re.compile(r"uses: [\w.-]+/[\w./-]+@[0-9a-f]{40} # v\d+(?:\.\d+){0,2}$")
EXACT_PIN = re.compile(r"([A-Za-z0-9_.-]+)==\d[\w.]*")


def load(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def triggers(workflow: dict[str, Any]) -> set[str]:
    # YAML 1.1 reads the key `on` as the boolean True.
    return set(workflow[True] if True in workflow else workflow["on"])


def steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return list(job.get("steps", []))


def workflow_names() -> list[str]:
    return sorted(path.name for path in WORKFLOWS.glob("*.y*ml"))


def test_exactly_the_ci_and_validate_workflows_exist() -> None:
    assert workflow_names() == ["ci.yml", "validate.yml"]


@pytest.mark.parametrize("name", ["ci.yml", "validate.yml"])
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


@pytest.mark.parametrize("name", ["ci.yml", "validate.yml"])
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


def test_dependabot_watches_pip_and_the_workflow_actions_weekly() -> None:
    updates = dependabot_updates()
    assert set(updates) == {"pip", "github-actions"}
    for entry in updates.values():
        assert entry["directory"] == "/"
        assert entry["schedule"] == {"interval": "weekly"}


def test_dependabot_leaves_the_home_assistant_test_stack_to_the_baseline() -> None:
    ignored = {
        rule["dependency-name"]: rule for rule in dependabot_updates()["pip"]["ignore"]
    }
    assert set(ignored) == HOME_ASSISTANT_STACK
    # A rule without update-types would also block the package's security updates.
    for name, rule in ignored.items():
        assert set(rule["update-types"]) == SEMVER_TYPES, name


def test_every_test_requirement_is_pinned_exactly() -> None:
    text = (ROOT / "requirements-test.txt").read_text(encoding="utf-8")
    lines = [line.strip() for line in text.splitlines()]
    names = set()
    for line in lines:
        if not line or line.startswith("#"):
            continue
        match = EXACT_PIN.fullmatch(line)
        assert match, f"requirements-test.txt: {line} is not an exact pin"
        names.add(match.group(1))
    assert names >= HOME_ASSISTANT_STACK


def test_the_integration_has_no_third_party_runtime_requirements() -> None:
    """Adding one needs a pip-audit gate for the runtime closure first."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    manifest = json.loads((INTEGRATION / "manifest.json").read_text(encoding="utf-8"))
    assert project["project"]["dependencies"] == []
    assert manifest["requirements"] == []
