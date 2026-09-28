"""Fixture capability: `nothing_to_do` skips with a reason, `no_reason`
skips without one, `good` succeeds. `prep` (setup) skips only when asked,
to show setup cannot be skipped."""

from __future__ import annotations

from runwhen_capability import Context, SkipTask, setup, task


@setup(outputs=[])
def prep(ctx: Context, skip: bool = False):
    if skip:
        raise SkipTask("setup has nothing to do")
    return {}


@task(outputs={})
def nothing_to_do(ctx: Context):
    raise SkipTask("no matching files in the diff")


@task(outputs={})
def no_reason(ctx: Context):
    raise SkipTask()


@task(outputs={"ok": "text"})
def good(ctx: Context):
    return {"ok": "fine"}
