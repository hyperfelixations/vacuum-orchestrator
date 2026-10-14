"""Record every scenario: `python -m tests.contract --record-to <dir>`.

See dev doc "Aufzeichnungen". Run from the repository root; the card's tests
call this to play the real integration.
"""

import argparse
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


def arguments(target: Path) -> list[str]:
    """The pytest run that writes the recordings into `target`."""
    return [
        str(HERE / "test_recordings.py"),
        "-k",
        "test_scenario_records",
        "--record-to",
        str(target.resolve()),
        "--no-cov",
        "-q",
        "-p",
        "no:cacheprovider",
    ]


def main(argv: list[str] | None = None) -> int:
    """Record and return pytest's exit code."""
    parser = argparse.ArgumentParser(prog="python -m tests.contract")
    parser.add_argument("--record-to", type=Path, required=True, metavar="DIR")
    target = parser.parse_args(argv).record_to
    if sys.platform == "win32":
        # HA supports only POSIX; see tests/stubs.
        sys.path.insert(0, str(HERE.parent / "stubs"))
    return int(pytest.main(arguments(target)))


if __name__ == "__main__":
    sys.exit(main())
