"""Stand-in for the `pymicro-vad` wheel, which Windows builds only with a compiler.

The E2E host never processes audio; see `tests/e2e_host/launch.py`.
"""


class MicroVad:
    """Voice activity detection is not available on the Windows E2E host."""

    def __init__(self) -> None:
        raise NotImplementedError("pymicro-vad is not installed")
