"""Run the connector.

    acme-mail-mcp                          # stdio, credentials from the environment
    acme-mail-mcp --transport http --port 8200

`ACME_MCP_DEFECTS=pagination-first-page-only acme-mail-mcp` starts the same
server with one seeded defect; see `defects.py` for the catalogue.
"""

from __future__ import annotations

import argparse

import anyio

from .config import Settings
from .server import build_from_settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Acme Mail MCP connector")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--port", type=int, default=8200)
    parser.add_argument("--base-url", dest="base_url", help="Acme Mail API base URL")
    parser.add_argument("--list-defects", action="store_true", help="print the defect catalogue")
    args = parser.parse_args()

    if args.list_defects:
        from .defects import REGISTRY

        for defect in REGISTRY.values():
            print(f"{defect.id:32} layer {defect.expected_layer}  {defect.title}")  # noqa: T201
        return

    overrides = {"upstream_base_url": args.base_url} if args.base_url else {}
    settings = Settings.from_env(**overrides)
    connector = build_from_settings(settings, with_auth=args.transport == "http")

    if args.transport == "stdio":
        anyio.run(connector.server.run_stdio_async)
        return

    import uvicorn

    # `host` decides the SDK's DNS-rebinding defaults, which is what the
    # `bind-all-interfaces` defect subverts. The listener itself always stays on
    # loopback so running the suite never exposes a port to the network.
    app = connector.server.streamable_http_app(host=connector.options.bind_host)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
