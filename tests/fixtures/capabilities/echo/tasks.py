"""Fixture capability: exercises ${setup.<name>} resolution and camelCase
manifest input -> snake_case kwarg mapping (userName -> user_name)."""

from __future__ import annotations

from runwhen_capability import Context, setup, task


@setup(outputs=["greeting", "items"])
def prep(ctx: Context, user_name: str):
    return {"greeting": f"hello {user_name}", "items": ["a", "b"]}


@task(outputs={"result": "text"})
def echo(ctx: Context, greeting: str, items: list[str]):
    return {"result": f"{greeting}:{','.join(items)}"}
