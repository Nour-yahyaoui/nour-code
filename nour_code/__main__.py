"""Command line entry point: ``python -m nour_code`` or ``nour-code``.

Examples::

    python -m nour_code                       # serve on 0.0.0.0:8000
    python -m nour_code --port 9000 --model qwen3:8b
    python -m nour_code --chat                # talk to the agent in this terminal
    python -m nour_code --doctor              # diagnose Python, port, Ollama and the model
    python -m nour_code --print-config
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .config import APP_NAME, Settings, load_settings
from .version_check import ensure_python_version


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=APP_NAME,
        description="Local AI coding agent server (Qwen3 8B on Ollama).",
    )
    parser.add_argument("--host", default=None, help="bind address (NOUR_CODE_HOST)")
    parser.add_argument("--port", type=int, default=None, help="bind port (NOUR_CODE_PORT)")
    parser.add_argument("--model", default=None, help="Ollama model name (NOUR_CODE_MODEL)")
    parser.add_argument(
        "--ollama-base-url", default=None, help="Ollama base URL (NOUR_CODE_OLLAMA_BASE_URL)"
    )
    parser.add_argument("--log-level", default=None, help="DEBUG, INFO, WARNING, ERROR")
    parser.add_argument("--reload", action="store_true", help="reload on code changes (development)")
    parser.add_argument("--workers", type=int, default=1, help="number of uvicorn workers")
    parser.add_argument("--root-path", default=None, help="ASGI root path when proxied")
    parser.add_argument(
        "--print-config",
        action="store_true",
        help="print the resolved configuration as JSON and exit",
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="check Python, dependencies, the API port and Ollama, then exit",
    )
    parser.add_argument(
        "--chat",
        action="store_true",
        help="talk to the agent in this terminal instead of starting the server",
    )
    return parser


def settings_from_args(args: argparse.Namespace) -> Settings:
    overrides: dict[str, Any] = {}
    for key, field_name in (
        ("host", "host"),
        ("port", "port"),
        ("model", "model"),
        ("ollama_base_url", "ollama_base_url"),
        ("log_level", "log_level"),
        ("root_path", "root_path"),
    ):
        value = getattr(args, key, None)
        if value is not None:
            overrides[field_name] = value
    return load_settings(**overrides)


def main(argv: list[str] | None = None) -> int:
    ensure_python_version()
    args = build_parser().parse_args(argv)
    settings = settings_from_args(args)

    if args.print_config:
        print(json.dumps(settings.describe(), indent=2, sort_keys=True))
        return 0

    if args.doctor:
        from .doctor import run_doctor

        return run_doctor(settings)

    if args.chat:
        from .chat import main as chat_main

        return chat_main()

    import uvicorn

    uvicorn.run(
        "nour_code.main:app",
        factory=False,
        host=settings.host,
        port=settings.port,
        reload=args.reload,
        workers=None if args.reload else max(1, args.workers),
        log_level=settings.log_level.lower(),
        root_path=settings.root_path,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
