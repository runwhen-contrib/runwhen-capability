"""Fixture capability: `big` returns a 4 KiB text output, so a request with a
small maxResultBytes overflows the inline result cap."""

from __future__ import annotations

from runwhen_capability import Context, task


@task(outputs={"blob": "text"})
def big(ctx: Context):
    return {"blob": "x" * 4096}
