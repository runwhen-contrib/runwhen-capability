"""Fixture capability: `boom` always raises; `good` always succeeds. Used to
verify one task's exception becomes a failed entry without stopping the
rest of the request."""

from __future__ import annotations

from runwhen_capability import Context, task


@task(outputs={})
def boom(ctx: Context):
    raise RuntimeError("kaboom")


@task(outputs={"ok": "text"})
def good(ctx: Context):
    return {"ok": "fine"}
