"""The catalogue of checks, and the machinery that runs them.

Importing this package registers every check, which is why the imports below
look unused.
"""

from . import contracts, http, protocol  # noqa: F401 - registration side effect
from .model import (
    Check,
    Ctx,
    Outcome,
    Profile,
    SpecRef,
    all_checks,
    failed,
    passed,
    register,
    selected,
    skipped,
)
from .runner import CheckRun, Report, run_checks, supported_eras

__all__ = [
    "Check",
    "CheckRun",
    "Ctx",
    "Outcome",
    "Profile",
    "Report",
    "SpecRef",
    "all_checks",
    "failed",
    "passed",
    "register",
    "run_checks",
    "selected",
    "skipped",
    "supported_eras",
]
