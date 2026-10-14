"""Python version guard — nour-code requires 3.11+ (``tomllib``, ``Self``, ...)."""

from __future__ import annotations

import sys

MINIMUM = (3, 11)


def ensure_python_version(minimum: tuple[int, int] = MINIMUM) -> None:
    """Exit with a clear message when running on an unsupported interpreter."""
    if sys.version_info[:2] < minimum:
        required = ".".join(str(part) for part in minimum)
        current = ".".join(str(part) for part in sys.version_info[:3])
        sys.stderr.write(
            f"nour-code requires Python {required}+ (running {current}).\n"
            f"Install a newer interpreter, then: pip install -r requirements.txt\n"
        )
        raise SystemExit(2)
