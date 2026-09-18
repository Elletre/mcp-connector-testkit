"""Run the fake provider standalone: `acme-mail-api --port 8100 --control`."""

from __future__ import annotations

import argparse

import uvicorn

from .app import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Acme Mail provider (a test double)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8100)
    parser.add_argument(
        "--control",
        action="store_true",
        help="expose the /_control plane (fault injection, audit log, state snapshots)",
    )
    args = parser.parse_args()
    uvicorn.run(create_app(control_enabled=args.control), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
