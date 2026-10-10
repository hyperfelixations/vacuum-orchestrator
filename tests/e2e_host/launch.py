"""Run Home Assistant for the E2E host: `python launch.py <config dir>`.

Started as a script so the repository root stays off `sys.path`; HA must load
`custom_components` from the config directory. See dev doc "E2E-Host".
"""

import runpy
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main(config: str) -> None:
    """Start HA in the foreground until `homeassistant.stop`."""
    arguments = ["hass", "-c", config, "--skip-pip"]
    if sys.platform == "win32":
        # HA supports only POSIX; local Windows runs need the POSIX modules HA
        # imports and the wheels of assist_pipeline that need a compiler.
        sys.path += [str(HERE.parent / "stubs"), str(HERE / "windows")]
        # Imported only now: HA needs the stand-ins on the path first.
        from homeassistant.helpers import signal  # noqa: PLC0415

        # The Proactor loop has no signal handlers; the host stops HA by action.
        signal.async_register_signal_handling = lambda hass: None
        arguments.append("--ignore-os-check")
    sys.argv = arguments
    runpy.run_module("homeassistant", run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main(sys.argv[1])
