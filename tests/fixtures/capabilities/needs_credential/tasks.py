"""Fixture capability: `need_token` requires a declared credential named
"token" and echoes back whatever Context.credential() resolves it to (or
None). Used to exercise the run_request(allow_anonymous_credentials=...)
escape hatch at the host level without any git plumbing."""

from __future__ import annotations

from runwhen_capability import Context, task


@task(outputs={"token": "text"})
def need_token(ctx: Context):
    return {"token": ctx.credential("token")}
