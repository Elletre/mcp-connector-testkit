#!/usr/bin/env python
"""Render the README's terminal image from a real run.

    uv run python tools/render_screenshot.py

Runs layers 1 and 2 against the demo connector (stateless era, stdio) with the
same profile the test suite uses, prints the report exactly as `mcpqa check`
would, and saves that console output as docs/assets/check-report.svg. Nothing
in the image is typed by hand.
"""

from __future__ import annotations

import sys
from pathlib import Path

from rich.console import Console

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from acme_mail_api.server import ProviderServer  # noqa: E402
from mcpqa.checks import run_checks  # noqa: E402
from mcpqa.report.render import print_report  # noqa: E402
from support import demo_profile, stdio_target  # noqa: E402


def main() -> None:
    console = Console(record=True, width=150, force_terminal=True, color_system="truecolor")
    with ProviderServer() as provider:
        report = run_checks(stdio_target(provider), demo_profile(provider), eras=("stateless",))
    print_report(report, console)
    out = ROOT / "docs/assets/check-report.svg"
    out.parent.mkdir(parents=True, exist_ok=True)
    console.save_svg(str(out), title="mcpqa check --stdio acme-mail-mcp")
    print(f"wrote {out.relative_to(ROOT)}", file=sys.stderr)


if __name__ == "__main__":
    main()
