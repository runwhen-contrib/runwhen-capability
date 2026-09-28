"""runwhen_capability -- the SDK a capability repo's tasks are written against.

Tasks are plain Python functions; the SDK owns every boundary -- inputs,
outputs, credentials, subprocesses, storage. `rwtask` (cli.py) is the task
host that runs them, in a pod (`rwtask serve`) or locally (`rwtask run`).
"""

# The one place the package version is written down: pyproject.toml reads it
# (setuptools dynamic version) and `rwtask --version` prints it.
__version__ = "0.2.0"

from .context import Context
from .decorators import setup, task
from .errors import SkipTask
from .models import (
    Bundle,
    BundleFile,
    BundleRequestEnvelope,
    BundleTarget,
    Finding,
    GrepMatch,
    GrepResult,
    LsEntry,
    LsResult,
    ReadResult,
    RequestEnvelope,
    ResultEnvelope,
    SetupResult,
    SetupSpec,
    TaskHostRequest,
    TaskHostResult,
    TaskResult,
    TaskSpec,
)

__all__ = [
    "Context",
    "setup",
    "task",
    "SkipTask",
    "Finding",
    "GrepMatch",
    "GrepResult",
    "LsEntry",
    "LsResult",
    "ReadResult",
    "RequestEnvelope",
    "ResultEnvelope",
    "SetupResult",
    "SetupSpec",
    "TaskHostRequest",
    "TaskHostResult",
    "TaskResult",
    "TaskSpec",
    "Bundle",
    "BundleFile",
    "BundleRequestEnvelope",
    "BundleTarget",
]
