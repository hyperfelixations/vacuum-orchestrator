"""Stand-in for the `pyspeex-noise` wheel, which Windows builds only with a compiler.

The E2E host never processes audio; see `tests/e2e_host/launch.py`.
"""


class AudioProcessor:
    """Noise suppression is not available on the Windows E2E host."""

    def __init__(self, *_args: object) -> None:
        raise NotImplementedError("pyspeex-noise is not installed")
