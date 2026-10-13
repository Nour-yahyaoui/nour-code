"""nour-code — a production-grade local AI coding agent server.

The package is deliberately small and explicit: :mod:`nour_code.main` wires the
pieces together (FastAPI app + dependency injection), :mod:`nour_code.agent`
runs the tool-calling loop, and everything else is a replaceable component.
"""

from __future__ import annotations

__version__ = "1.0.0"
__app_name__ = "nour-code"

__all__ = ["__version__", "__app_name__"]
