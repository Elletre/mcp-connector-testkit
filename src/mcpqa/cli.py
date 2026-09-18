"""The `mcpqa` command.

mcpqa check --stdio "python -m my_server"
mcpqa check --http https://example.com/mcp --bearer "$TOKEN" --expects-auth
mcpqa list-checks
mcpqa evals run --environment evals/acme_environment.py:make --model ollama:llama3:latest
mcpqa evals compare evals/results/baseline evals/results/candidate
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
import time
from collections.abc import Callable
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


# --------------------------------------------------------------------- evals


def _load_factory(spec: str) -> Callable[[], Any]:
    path_text, _, name = spec.partition(":")
    path = Path(path_text).resolve()
    module_spec = importlib.util.spec_from_file_location(path.stem, path)
    if module_spec is None or module_spec.loader is None:
        raise SystemExit(f"cannot load environment from {path}")
    module = importlib.util.module_from_spec(module_spec)
    sys.path.insert(0, str(path.parent))
    sys.modules[module_spec.name] = module  # dataclasses look their module up by name
    module_spec.loader.exec_module(module)
    factory = getattr(module, name or "make", None)
    if factory is None:
        raise SystemExit(f"{path} has no factory named {name or 'make'!r}")
    return factory  # type: ignore[no-any-return]


def _adapter_factory(
    model: str, recordings: list[dict[str, Any]] | None, temperature: float | None = None
) -> Callable[[int], Any]:
    provider, _, name = model.partition(":")
    if temperature is not None and provider not in ("ollama", "openai"):
        raise SystemExit(f"--temperature applies to ollama and openai models, not {provider}")

    def build(repeat: int) -> Any:
        if provider == "ollama":
            from .evals.adapters.prompted_json import PromptedJsonAdapter

            # A seed per repeat: sampled runs vary between repeats, and a re-run
            # of the same repeat samples the same way.
            adapter: Any = PromptedJsonAdapter(
                model=name or "llama3:latest", temperature=temperature or 0.0, seed=repeat
            )
        elif provider == "openai":
            from .evals.adapters.openai_compat import OpenAICompatibleAdapter

            adapter = OpenAICompatibleAdapter(model=name, temperature=temperature or 0.0)
        elif provider == "anthropic":
            from .evals.adapters.anthropic_messages import DEFAULT_MODEL, AnthropicAdapter

            adapter = AnthropicAdapter(model=name or DEFAULT_MODEL)
        elif provider == "replay":
            from .evals.adapters.replay import ReplayAdapter

            return ReplayAdapter(recordings=ReplayAdapter.load(Path(name)), repeat=repeat)
        else:
            raise SystemExit(f"unknown model provider {provider!r} (ollama, openai, anthropic, replay)")
        if recordings is None:
            return adapter
        from .evals.adapters.replay import RecordingAdapter

        return RecordingAdapter(inner=adapter, sink=recordings, repeat=repeat)

    return build


def cmd_evals_run(args: argparse.Namespace) -> int:
    from .evals import report
    from .evals.dataset import load_cases
    from .evals.runner import run

    console = Console()
    cases = load_cases(Path(args.cases))
    if args.only:
        wanted = set(args.only.split(","))
        cases = [case for case in cases if case.id in wanted or case.category in wanted]
    environment = _load_factory(args.environment)()
    replaying = args.model.startswith("replay:")
    recordings: list[dict[str, Any]] | None = None if replaying else []

    slug = re.sub(r"[^A-Za-z0-9.-]+", "-", f"{args.model}-{environment.label}").strip("-")
    out = Path(args.out) if args.out else Path("evals/results") / f"{time.strftime('%Y-%m-%d')}-{slug}"
    out.mkdir(parents=True, exist_ok=True)
    trials_path, turns_path = out / "trials.jsonl", out / "turns.jsonl"

    prior = report.load_trials(trials_path) if args.resume else []
    if prior:
        console.print(f"[dim]resuming: {len(prior)} trial(s) already recorded in {out}[/dim]")
        prior = [trial for trial in prior if trial.error is None]
    else:
        trials_path.write_text("")
        turns_path.write_text("")

    def show(trial: Any) -> None:
        # Written as it happens, so an interrupted run keeps what it has.
        with trials_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(trial.as_dict(), ensure_ascii=False) + "\n")
        if recordings:
            with turns_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(recordings[-1], ensure_ascii=False) + "\n")
        if trial.error:
            console.print(f"[yellow]•[/yellow] r{trial.repeat} {trial.case_id} could not run: {trial.error}")
            return
        mark = (
            "[green]✓[/green]"
            if trial.passed
            else ("[bold red]✗ unsafe[/bold red]" if trial.unsafe else "[red]✗[/red]")
        )
        detail = "" if trial.passed else f" — {trial.failures[0]}"
        if trial.retried_after:
            detail += f" [second attempt; the first failed with {trial.retried_after}]"
        console.print(f"{mark} r{trial.repeat} {trial.case_id} ({trial.seconds}s){detail}", highlight=False)

    result = run(
        cases,
        environment,
        _adapter_factory(args.model, recordings, args.temperature),
        repeats=args.repeats,
        max_steps=args.max_steps,
        on_trial=show,
        prior=prior,
    )
    # Rewrite the trial log in case order, errors kept; the summary excludes them.
    trials_path.write_text(
        "".join(json.dumps(trial.as_dict(), ensure_ascii=False) + "\n" for trial in result.trials)
    )
    summary = report.write_summary(result, out)
    if result.stopped_early:
        console.print(f"[bold yellow]stopped early:[/bold yellow] {result.stopped_early}")
        console.print(f"re-run with --resume --out {out} to continue")
    rate = summary["pass_rate"]
    console.print(
        f"\n[bold]{summary['model']}[/bold] on {summary['environment']}: "
        f"{rate['estimate']:.0%} [{rate['low']:.0%}–{rate['high']:.0%}] · "
        f"pass^k {summary['pass_hat_k']:.0%} · {summary['errored_trials']} errored · results in {out}"
    )
    return 2 if result.stopped_early else 0


def cmd_evals_compare(args: argparse.Namespace) -> int:
    from .evals import report

    baseline = report.load(Path(args.baseline))
    candidate = report.load(Path(args.candidate))
    comparison = report.compare(baseline, candidate)
    console = Console()
    console.print(
        f"overall change: {comparison.overall.estimate:+.0%} "
        f"[{comparison.overall.low:+.0%}, {comparison.overall.high:+.0%}]"
    )
    for name, delta in comparison.categories.items():
        flag = "  [red]regressed[/red]" if delta.high < 0 else ""
        console.print(f"  {name:20} {delta.estimate:+.0%} [{delta.low:+.0%}, {delta.high:+.0%}]{flag}")
    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {
                    "baseline": str(args.baseline),
                    "candidate": str(args.candidate),
                    "overall": vars(comparison.overall),
                    "categories": {name: vars(delta) for name, delta in comparison.categories.items()},
                    "regressed": comparison.regressed,
                    "regressed_categories": comparison.regressed_categories(),
                },
                indent=2,
            )
            + "\n"
        )
    return 1 if comparison.regressed and args.gate else 0


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

    evals = commands.add_parser("evals", help="agent-level evaluation")
    evals_commands = evals.add_subparsers(dest="evals_command", required=True)
    evals_run = evals_commands.add_parser("run", help="run the cases against a model")
    evals_run.add_argument("--environment", required=True, help="path/to/module.py:factory")
    evals_run.add_argument("--cases", default="evals/cases")
    evals_run.add_argument(
        "--model",
        default="ollama:llama3:latest",
        help="ollama:NAME, openai:NAME, anthropic:NAME, replay:PATH",
    )
    evals_run.add_argument("--repeats", type=int, default=3)
    evals_run.add_argument(
        "--temperature", type=float, help="sampling temperature (ollama, openai); default 0"
    )
    evals_run.add_argument("--max-steps", type=int, default=6)
    evals_run.add_argument("--only", help="comma-separated case ids or categories")
    evals_run.add_argument("--out", help="results directory")
    evals_run.add_argument(
        "--resume", action="store_true", help="keep finished trials in --out and run the rest"
    )
    evals_run.set_defaults(handler=cmd_evals_run)

    evals_compare = evals_commands.add_parser("compare", help="compare two runs, case by case")
    evals_compare.add_argument("baseline")
    evals_compare.add_argument("candidate")
    evals_compare.add_argument("--json", help="write the comparison as JSON")
    evals_compare.add_argument("--gate", action="store_true", help="exit 1 on a significant regression")
    evals_compare.set_defaults(handler=cmd_evals_compare)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
