"""The `mcpqa` command.

mcpqa check --stdio "python -m my_server"
mcpqa check --http https://example.com/mcp --bearer "$TOKEN" --expects-auth
mcpqa list-checks
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from rich.console import Console

from .checks import Profile, all_checks, run_checks, supported_eras
from .protocol import Era
from .report.render import dumps, print_report, to_markdown
from .target import HttpTarget, StdioTarget, Target

# --------------------------------------------------------------------- check


def _target(args: argparse.Namespace) -> Target:
    if args.stdio:
        return StdioTarget.parse(args.stdio)
    headers = dict(header.split(":", 1) for header in args.header or [])
    return HttpTarget(
        url=args.http, bearer=args.bearer, headers={k.strip(): v.strip() for k, v in headers.items()}
    )


def _profile(args: argparse.Namespace) -> Profile:
    samples: dict[str, list[dict[str, Any]]] = {}
    if args.samples:
        samples = json.loads(Path(args.samples).read_text(encoding="utf-8"))
    return Profile(
        samples=samples,
        secrets=tuple(args.secret or ()),
        expects_auth=args.expects_auth,
        foreign_tokens=tuple(args.foreign_token or ()),
        allow_mutations=args.allow_mutations,
    )


def cmd_check(args: argparse.Namespace) -> int:
    console = Console(stderr=False)
    target = _target(args)
    if args.era == "auto":
        eras: tuple[Era, ...] = supported_eras(target)
        console.print(f"[dim]eras answered: {', '.join(eras)}[/dim]")
    elif args.era == "both":
        eras = ("stateless", "handshake")
    else:
        eras = (args.era,)
    layers = tuple(int(layer) for layer in args.layers.split(",")) if args.layers else None
    ids = tuple(item.strip() for item in args.only.split(",")) if args.only else None

    report = run_checks(target, _profile(args), eras=eras, ids=ids, layers=layers)
    print_report(report, console, verbose=args.verbose)
    if args.json:
        Path(args.json).write_text(dumps(report) + "\n", encoding="utf-8")
    if args.markdown:
        Path(args.markdown).write_text(to_markdown(report), encoding="utf-8")
    failing = report.errors + (report.warnings if args.fail_on == "warning" else [])
    return 1 if failing else 0


def cmd_list_checks(args: argparse.Namespace) -> int:
    checks = [check for check in all_checks() if args.layer is None or check.layer == args.layer]
    if args.markdown:
        print("| ID | Layer | Severity | Check | Eras | Transports | Specification |")
        print("| --- | :-: | --- | --- | --- | --- | --- |")
        for check in checks:
            print(
                f"| {check.id} | {check.layer} | {check.severity} | {check.title} | "
                f"{', '.join(check.eras)} | {', '.join(check.transports)} | "
                f"[{check.ref.version}]({check.ref.url}) |"
            )
        return 0
    console = Console()
    for check in checks:
        console.print(f"[bold]{check.id}[/bold] [{check.severity}] {check.title}")
        console.print(f"    {check.ref.describe()}", style="dim", markup=False)
        if check.escalation:
            console.print(f"    stricter than the keyword: {check.escalation}", style="dim", markup=False)
    return 0


# ---------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mcpqa", description="A five-layer test kit for MCP connectors.")
    commands = parser.add_subparsers(dest="command", required=True)

    check = commands.add_parser("check", help="run the conformance checks against a server")
    where = check.add_mutually_exclusive_group(required=True)
    where.add_argument("--stdio", help="command that starts the server")
    where.add_argument("--http", help="URL of the MCP endpoint")
    check.add_argument("--bearer", help="token for an HTTP endpoint")
    check.add_argument("--header", action="append", help="extra HTTP header, 'Name: value'")
    check.add_argument("--era", choices=["auto", "stateless", "handshake", "both"], default="auto")
    check.add_argument("--layers", help="comma-separated layers, e.g. 1 or 1,2")
    check.add_argument("--only", help="comma-separated check ids or prefixes, e.g. P-,H-001")
    check.add_argument("--samples", help="JSON file: tool name -> list of argument objects")
    check.add_argument("--allow-mutations", action="store_true", help="call tools that change state")
    check.add_argument("--expects-auth", action="store_true", help="the endpoint should demand a token")
    check.add_argument("--foreign-token", action="append", help="a token issued for something else")
    check.add_argument("--secret", action="append", help="a value that must never appear in output")
    check.add_argument("--json", help="write the report as JSON")
    check.add_argument("--markdown", help="write the report as Markdown")
    check.add_argument("--fail-on", choices=["error", "warning"], default="error")
    check.add_argument("-v", "--verbose", action="store_true", help="print the evidence for failures")
    check.set_defaults(handler=cmd_check)

    listing = commands.add_parser("list-checks", help="print the check catalogue")
    listing.add_argument("--layer", type=int)
    listing.add_argument("--markdown", action="store_true")
    listing.set_defaults(handler=cmd_list_checks)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
