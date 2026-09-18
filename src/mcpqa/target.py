"""How to reach the server under test."""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class StdioTarget:
    """A server started as a subprocess and spoken to over its standard streams."""

    command: list[str]
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    name: str = "stdio"

    @classmethod
    def parse(cls, command: str, **kwargs: object) -> StdioTarget:
        return cls(command=shlex.split(command), **kwargs)  # type: ignore[arg-type]

    def describe(self) -> str:
        # The executable's name, not its path: reports get pasted into tickets
        # and READMEs, and a home directory has no business in either.
        program, *arguments = self.command or [""]
        return f"stdio: {' '.join([Path(program).name, *arguments])}"


@dataclass(frozen=True)
class HttpTarget:
    """A server reached over the streamable HTTP transport."""

    url: str
    bearer: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    name: str = "http"
    origin: str | None = None
    """Base origin for well-known documents; derived from `url` when unset."""

    def describe(self) -> str:
        return f"http: {self.url}"

    def base_origin(self) -> str:
        if self.origin:
            return self.origin.rstrip("/")
        scheme, _, rest = self.url.partition("://")
        authority = rest.split("/", 1)[0]
        return f"{scheme}://{authority}"


Target = StdioTarget | HttpTarget
