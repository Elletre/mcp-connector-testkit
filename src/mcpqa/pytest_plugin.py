"""pytest integration for mcpqa.

Registered through the `pytest11` entry point, so installing the kit is enough
to get its fixtures, markers and the secret-leak guard in any suite.
"""

from __future__ import annotations

import pytest

MARKERS = {
    "layer1": "protocol and transport conformance",
    "layer2": "tool contracts and annotation truthfulness",
    "layer3": "authorization, tenancy and secret handling",
    "layer4": "upstream API behaviour",
    "layer5": "agent-level evaluation (needs a model; not part of the PR gate)",
}


def pytest_configure(config: pytest.Config) -> None:
    for name, description in MARKERS.items():
        config.addinivalue_line("markers", f"{name}: {description}")
