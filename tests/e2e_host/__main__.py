"""Run the E2E host until stdin closes: `python -m tests.e2e_host --help`.

Prints one JSON line once HA is ready: its URL, the session file with the
owner's short-lived tokens and the config directory. See dev doc "E2E-Host".
"""

import argparse
import json
import sys
from contextlib import suppress
from pathlib import Path

from tests.e2e_host.host import HOUSEHOLDS, SESSION, running


def main() -> None:
    """Start the host, report it and stop it when the caller closes stdin."""
    parser = argparse.ArgumentParser(prog="python -m tests.e2e_host")
    parser.add_argument(
        "--household", choices=sorted(HOUSEHOLDS), default="single_roborock"
    )
    parser.add_argument(
        "--install-voi", action="store_true", help="add VOI through its config flow"
    )
    parser.add_argument(
        "--card", type=Path, help="the built card to show on the dashboard"
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="keep the config here instead of a temporary directory",
    )
    options = parser.parse_args()
    with running(
        household=options.household,
        install_voi=options.install_voi,
        card=options.card and options.card.resolve(),
        config=options.config and options.config.resolve(),
    ) as host:
        report = {
            "url": host.url,
            "session": str(host.config / SESSION),
            "config": str(host.config),
        }
        print(json.dumps(report), flush=True)
        with suppress(KeyboardInterrupt):
            sys.stdin.read()


if __name__ == "__main__":
    main()
