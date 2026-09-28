"""@setup and @task -- the registry a capability's tasks.py populates by
being imported. Registration is scoped to one Registry per capability-module
load (via a contextvar), not a process-wide global: two capabilities (or two
test fixtures) loaded in the same process never see each other's tasks.
"""

from __future__ import annotations

import contextvars
from collections.abc import Callable
from dataclasses import dataclass

_current_registry: contextvars.ContextVar[Registry | None] = contextvars.ContextVar(
    "runwhen_capability_current_registry", default=None
)


@dataclass
class SetupDef:
    name: str
    func: Callable
    outputs: list[str]


@dataclass
class TaskDef:
    name: str
    func: Callable
    outputs: dict[str, str]


class Registry:
    """One capability module's registered setup/task functions, keyed by
    function name -- the name a manifest's `setup.task` / `tasks[].name`
    refers to."""

    def __init__(self) -> None:
        self.setups: dict[str, SetupDef] = {}
        self.tasks: dict[str, TaskDef] = {}


def setup(outputs: list[str]):
    """`@setup(outputs=[...])` -- registers a request-scoped setup function.
    Its declared `outputs` are the names tasks reference as
    `${setup.<name>}`."""

    def decorator(func: Callable) -> Callable:
        reg = _current_registry.get()
        if reg is not None:
            reg.setups[func.__name__] = SetupDef(
                name=func.__name__, func=func, outputs=list(outputs)
            )
        return func

    return decorator


def task(outputs: dict[str, str]):
    """`@task(outputs={name: kind})` -- registers a task function. `outputs`
    maps each returned output name to its semantic kind (e.g.
    "rw.findings.v1"), matching the manifest's `tasks[].outputs`."""

    def decorator(func: Callable) -> Callable:
        reg = _current_registry.get()
        if reg is not None:
            reg.tasks[func.__name__] = TaskDef(name=func.__name__, func=func, outputs=dict(outputs))
        return func

    return decorator
