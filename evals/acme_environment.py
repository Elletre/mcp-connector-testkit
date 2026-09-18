"""The evaluation environment for the demo connector.

This is the file a team writes for their own connector: how to reset the world
behind the server, how to read its state, how to inject the faults a case asks
for, and how to open a session. `mcpqa evals run` loads it by path:

    uv run mcpqa evals run --environment evals/acme_environment.py:make \\
        --cases evals/cases --model ollama:llama3:latest

Set `ACME_MCP_DEFECTS` to evaluate the connector with a seeded defect.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field

from acme_mail_api.control import ProviderControl
from acme_mail_api.server import ProviderServer
from mcpqa.evals.dataset import Fault
from mcpqa.session import Session
from mcpqa.target import StdioTarget


@dataclass
class AcmeEnvironment:
    defects: str = field(default_factory=lambda: os.environ.get("ACME_MCP_DEFECTS", ""))
    provider: ProviderServer = field(default_factory=ProviderServer)
    label: str = field(init=False)

    def __post_init__(self) -> None:
        self.provider.start()
        self.control = ProviderControl(self.provider.base_url)
        self.label = f"acme-mail{'+' + self.defects if self.defects else ''}"

    def reset(self) -> None:
        self.control.reset()

    def snapshot(self) -> frozenset[str]:
        return self.control.snapshot()

    def apply_faults(self, faults: tuple[Fault, ...]) -> None:
        self.control.clear()
        for fault in faults:
            if fault.status is not None:
                self.control.fail(
                    path=fault.path, status=fault.status, times=fault.times, headers=fault.headers
                )
            elif fault.delay_ms is not None:
                self.control.delay(path=fault.path, ms=fault.delay_ms, times=fault.times)

    def open_session(self) -> Session:
        target = StdioTarget(
            command=[sys.executable, "-m", "acme_mail_mcp"],
            env={
                "ACME_MAIL_BASE_URL": self.provider.base_url,
                "ACME_MAIL_ACCESS_TOKEN": "at_alice_rw",
                "ACME_MAIL_REFRESH_TOKEN": "rt_alice",
                "ACME_MAIL_ACCOUNT": "alice@acme.test",
                "ACME_MCP_DEFECTS": self.defects,
            },
            name="acme-mail",
        )
        return Session(target=target, protocol_version="2026-07-28").open()

    def close(self) -> None:
        self.provider.stop()


def make() -> AcmeEnvironment:
    return AcmeEnvironment()
