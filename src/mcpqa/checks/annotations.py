"""Layer 2, part two: are the tool annotations true?

`readOnlyHint`, `destructiveHint` and `idempotentHint` are the fields a client
consults before deciding whether to run a tool without asking. They are hints,
and the specification warns clients not to trust them from an untrusted server
— but a server that publishes a false one has still shipped a defect, and it is
one nothing else in the protocol will catch.

All three are checkable against observed state: call the tool, diff the facts
before and after, and compare that with what the tool said about itself.

    read-only   ⇔ nothing changed
    additive    ⇔ facts appeared, none disappeared
    destructive ⇔ a fact that existed is gone
    idempotent  ⇔ a second identical call changes nothing further
"""

from __future__ import annotations

from typing import Any

from ..probes import diff_states
from .model import Ctx, Outcome, failed, passed, register, skipped


def _call(name: str, arguments: dict[str, Any]) -> str:
    return f"{name}({', '.join(f'{key}={value!r}' for key, value in arguments.items())})"


def _annotations(tool: dict[str, Any]) -> dict[str, Any]:
    annotations = tool.get("annotations")
    return annotations if isinstance(annotations, dict) else {}


@register(
    id="A-001",
    title="Read-only tools do not change anything",
    layer=2,
    severity="error",
    needs=("oracle", "samples"),
)
def read_only_is_read_only(ctx: Ctx) -> Outcome:
    oracle = ctx.profile.oracle
    candidates = [
        tool
        for tool in ctx.tools()
        if _annotations(tool).get("readOnlyHint") is True
        and ctx.profile.safe_arguments_for(str(tool.get("name")))
    ]
    if not candidates:
        return skipped("no read-only tool has sample arguments")
    problems: list[str] = []
    for tool in candidates:
        name = str(tool.get("name"))
        for arguments in ctx.profile.safe_arguments_for(name):
            before = oracle.snapshot()
            ctx.session.call_tool(name, arguments)
            diff = diff_states(before, oracle.snapshot())
            if diff.changed:
                problems.append(f"{_call(name, arguments)} changed state: {diff.describe()}")
    if problems:
        return failed(
            "; ".join(problems[:3]) + " — a client that trusted readOnlyHint ran this without asking",
            ctx.evidence(),
        )
    return passed(f"{len(candidates)} read-only tool(s) left the state untouched")


@register(
    id="A-002",
    title="Tools that destroy things say so",
    layer=2,
    severity="error",
    needs=("oracle", "samples", "mutations"),
)
def destructive_is_declared(ctx: Ctx) -> Outcome:
    oracle = ctx.profile.oracle
    candidates = [
        tool
        for tool in ctx.tools()
        if _annotations(tool).get("readOnlyHint") is not True
        and ctx.profile.arguments_for(str(tool.get("name")))
    ]
    if not candidates:
        return skipped("no mutating tool has sample arguments")
    problems: list[str] = []
    observed = 0
    for tool in candidates:
        name = str(tool.get("name"))
        annotations = _annotations(tool)
        arguments = ctx.profile.arguments_for(name)[0]
        before = oracle.snapshot()
        ctx.session.call_tool(name, arguments)
        diff = diff_states(before, oracle.snapshot())
        observed += 1
        if diff.destructive and annotations.get("destructiveHint") is False:
            problems.append(
                f"{name} removed {len(diff.removed)} fact(s) while declaring destructiveHint=false "
                f"({diff.describe()})"
            )
    if problems:
        return failed(
            "; ".join(problems[:3]) + " — clients skip confirmation for tools that claim to be additive",
            ctx.evidence(),
        )
    return passed(f"{observed} mutating tool(s) match their destructiveHint")


@register(
    id="A-003",
    title="Idempotent tools really are",
    layer=2,
    severity="error",
    needs=("oracle", "samples", "mutations"),
)
def idempotent_is_idempotent(ctx: Ctx) -> Outcome:
    oracle = ctx.profile.oracle
    candidates = [
        tool
        for tool in ctx.tools()
        if _annotations(tool).get("idempotentHint") is True
        and _annotations(tool).get("readOnlyHint") is not True
        and ctx.profile.arguments_for(str(tool.get("name")))
    ]
    if not candidates:
        return skipped("no mutating tool declares idempotentHint")
    problems: list[str] = []
    for tool in candidates:
        name = str(tool.get("name"))
        arguments = ctx.profile.arguments_for(name)[0]
        ctx.session.call_tool(name, arguments)
        settled = oracle.snapshot()
        ctx.session.call_tool(name, arguments)
        diff = diff_states(settled, oracle.snapshot())
        if diff.changed:
            problems.append(f"{name}: the second identical call changed state again ({diff.describe()})")
    if problems:
        return failed(
            "; ".join(problems[:3]) + " — a client that retries this tool will do the work twice",
            ctx.evidence(),
        )
    return passed(f"{len(candidates)} idempotent tool(s) were stable on a repeat call")


@register(
    id="A-004",
    title="Tools that change things carry annotations at all",
    layer=2,
    severity="info",
    needs=("oracle", "samples", "mutations"),
)
def mutating_tools_are_annotated(ctx: Ctx) -> Outcome:
    oracle = ctx.profile.oracle
    unannotated: list[str] = []
    for tool in ctx.tools():
        name = str(tool.get("name"))
        arguments = ctx.profile.arguments_for(name)
        if not arguments or _annotations(tool):
            continue
        before = oracle.snapshot()
        ctx.session.call_tool(name, arguments[0])
        if diff_states(before, oracle.snapshot()).changed:
            unannotated.append(name)
    if unannotated:
        return failed(
            f"these tools change state and publish no annotations, so a client has to guess: {unannotated}"
        )
    return passed("every tool observed changing state publishes annotations")
