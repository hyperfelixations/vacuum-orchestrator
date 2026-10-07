"""The release candidate workflow refuses unsafe requests and never publishes.

The embedded scripts are executed: the version gate against this repository and
the request validation against throwaway git repositories.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from base64 import b64encode
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")


def workflow() -> dict[str, Any]:
    loaded = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def step(job: str, name: str) -> dict[str, Any]:
    return next(
        item for item in workflow()["jobs"][job]["steps"] if item.get("name") == name
    )


def test_only_the_owner_dispatches_and_only_the_draft_job_may_write() -> None:
    loaded = workflow()
    # YAML 1.1 reads the key `on` as the boolean True.
    assert set(loaded[True]) == {"workflow_dispatch"}
    assert set(loaded[True]["workflow_dispatch"]["inputs"]) == {
        "version",
        "expected_sha",
        "release_kind",
    }
    assert loaded["permissions"] == {"contents": "read"}
    writers = {
        name
        for name, job in loaded["jobs"].items()
        if job.get("permissions", loaded["permissions"]).get("contents") == "write"
    }
    assert writers == {"create-draft"}
    assert set(loaded["jobs"]["create-draft"]["needs"]) == {
        "validate",
        "test",
        "hassfest",
        "hacs",
    }


def test_draft_is_unpublished_without_assets_and_names_the_tree() -> None:
    script = step(
        "create-draft", "Create the unpublished draft against the approved commit"
    )
    run = script["run"]
    assert "--draft" in run and '--target "$EXPECTED_SHA"' in run
    assert "gh release upload" not in run and "release-asset" not in run
    assert "Tree hash: \\`$TREE_HASH\\`" in run
    for job in ("test", "hassfest", "create-draft"):
        checkout = workflow()["jobs"][job]["steps"][0]
        assert checkout["with"]["ref"] == "${{ needs.validate.outputs.release_sha }}"


def version_gate(version: str) -> subprocess.CompletedProcess[str]:
    run = step("test", "Check version files and brand icon")["run"]
    source = run.split("<<'EOF'\n", 1)[1].rsplit("EOF", 1)[0]
    return subprocess.run(
        [sys.executable, "-", version],
        input=source,
        capture_output=True,
        check=False,
        cwd=ROOT,
        text=True,
    )


def test_version_gate_accepts_this_repository_and_rejects_other_versions() -> None:
    accepted = version_gate("0.1.1")
    assert accepted.returncode == 0, accepted.stderr
    rejected = version_gate("0.1.0")
    assert rejected.returncode != 0
    assert "expected '0.1.0'" in rejected.stderr


class Repository:
    """A throwaway history with `main` and one side commit."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.mkdir()
        self.git("init", "-q", "-b", "main")
        tree = self.git("mktree", input="")
        self.first = self.commit(tree, "first")
        self.second = self.commit(tree, "second", self.first)
        self.side = self.commit(tree, "side", self.first)
        self.git("update-ref", "refs/heads/main", self.second)

    def git(self, *args: str, input: str | None = None) -> str:
        identity = ["-c", "user.name=Release test", "-c", "user.email="]
        return subprocess.run(
            ["git", *identity, "-c", "commit.gpgsign=false", *args],
            input=input,
            capture_output=True,
            check=True,
            cwd=self.path,
            text=True,
        ).stdout.strip()

    def commit(self, tree: str, message: str, parent: str | None = None) -> str:
        parents = ["-p", parent] if parent else []
        return self.git("commit-tree", tree, *parents, "-m", message)

    def tag(self, name: str, commit: str) -> None:
        self.git("tag", name, commit)


@pytest.fixture
def repository(tmp_path: Path) -> Repository:
    return Repository(tmp_path / "repository")


def validate(
    repository: Repository, tmp_path: Path, **overrides: str
) -> tuple[subprocess.CompletedProcess[str], str]:
    if POWERSHELL is None:
        pytest.skip("PowerShell is not installed")
    output = tmp_path / "github_output"
    output.write_text("", encoding="utf-8")
    values = {
        "ACTUAL_SHA": repository.second,
        "EXPECTED_SHA": repository.second,
        "RELEASE_KIND": "stable",
        "RELEASE_VERSION": "0.1.0",
        "REPOSITORY_OWNER": "owner",
        "SELECTED_REF": "refs/heads/main",
        "TRIGGERING_ACTOR": "owner",
        "GITHUB_OUTPUT": str(output),
    } | overrides
    prelude = "".join(f"$env:{key} = '{value}'\n" for key, value in values.items())
    run = step("validate", "Validate branch, SHA and version")["run"]
    command = b64encode((prelude + run).encode("utf-16-le")).decode()
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-EncodedCommand", command],
        capture_output=True,
        check=False,
        cwd=repository.path,
        encoding="utf-8",
        errors="replace",
    )
    return result, output.read_text(encoding="utf-8")


def test_valid_stable_request_writes_tag_title_and_sha(
    repository: Repository, tmp_path: Path
) -> None:
    result, output = validate(
        repository, tmp_path, EXPECTED_SHA=repository.second.upper()
    )
    assert result.returncode == 0, result.stderr
    assert output.splitlines() == [
        "tag=v0.1.0",
        "title=Vacuum Orchestrator 0.1.0",
        f"sha={repository.second}",
    ]


def test_dev_request_after_a_stable_release(
    repository: Repository, tmp_path: Path
) -> None:
    repository.tag("v0.1.0", repository.first)
    result, output = validate(
        repository, tmp_path, RELEASE_KIND="dev", RELEASE_VERSION="0.1.1-dev.1"
    )
    assert result.returncode == 0, result.stderr
    assert "tag=v0.1.1-dev.1" in output


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"TRIGGERING_ACTOR": "someone"}, "Only the repository owner"),
        ({"SELECTED_REF": "refs/heads/feature"}, "branch 'main' selected"),
        ({"EXPECTED_SHA": "abc123"}, "complete 40-character commit SHA"),
        ({"EXPECTED_SHA": "0" * 40}, "does not exist"),
        ({"EXPECTED_SHA": "side"}, "not part of the main branch history"),
        ({"RELEASE_VERSION": "0.1.0-dev.1"}, "stable version must look like"),
        ({"RELEASE_VERSION": "01.1.0"}, "stable version must look like"),
        ({"RELEASE_KIND": "dev"}, "dev pre-release version must look like"),
        ({"RELEASE_KIND": "nightly"}, "Unsupported release kind"),
    ],
)
def test_unsafe_requests_are_refused(
    repository: Repository,
    tmp_path: Path,
    overrides: dict[str, str],
    message: str,
) -> None:
    if overrides.get("EXPECTED_SHA") == "side":
        overrides = overrides | {"EXPECTED_SHA": repository.side}
    result, output = validate(repository, tmp_path, **overrides)
    assert result.returncode != 0
    assert message in result.stdout + result.stderr
    assert output == ""


@pytest.mark.parametrize(
    ("tag", "commit", "version", "message"),
    [
        ("v0.1.0", "first", "0.1.0", "already exists"),
        ("v0.2.0", "first", "0.1.0", "must be higher than the existing release"),
        ("v0.0.9", "side", "0.1.0", "must contain every earlier stable release"),
    ],
)
def test_existing_releases_constrain_stable_versions(
    repository: Repository,
    tmp_path: Path,
    tag: str,
    commit: str,
    version: str,
    message: str,
) -> None:
    repository.tag(tag, getattr(repository, commit))
    result, _output = validate(repository, tmp_path, RELEASE_VERSION=version)
    assert result.returncode != 0
    assert message in result.stdout + result.stderr
