"""The reference time of a task run (CONTRACTS.md "What a script sees").

The executor takes one instant at the start of a run and gives it to the script as
`RW_NOW` (RFC 3339, UTC, millisecond precision, `Z`: `2026-11-04T09:14:02.123Z`) and
`RW_NOW_EPOCH` (integer seconds), so that anything a task puts in a command or an output
can be replayed (`rwtask test`) with the time it was recorded at. These are the three
conversions every host of a run (plugin/run.py, `runwhen_capability.testing`) and every
reader (`Context.now`, the fixture loader) shares.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

NOW_ENV = "RW_NOW"
NOW_EPOCH_ENV = "RW_NOW_EPOCH"

_FORMAT = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z")


def check_aware(now: datetime) -> datetime:
    """`now`, or ValueError when it has no timezone (which instant would it be?)."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError(f"the reference time must carry a timezone, got {now.isoformat()!r}")
    return now


def format_now(now: datetime) -> str:
    """`now` as `RW_NOW` carries it: UTC, truncated to the millisecond, `Z`."""
    utc = check_aware(now).astimezone(UTC)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def parse_now(text: str) -> datetime:
    """The aware UTC datetime for an `RW_NOW` value (UTC RFC 3339 with `Z`, the fraction
    optional). ValueError for anything else."""
    if not isinstance(text, str) or not _FORMAT.fullmatch(text):
        raise ValueError(f"{text!r} is not a UTC RFC 3339 timestamp like 2026-11-04T09:14:02.123Z")
    return datetime.fromisoformat(text).astimezone(UTC)


def current_now() -> datetime:
    """The wall clock, truncated to the millisecond: what `RW_NOW` can say."""
    now = datetime.now(UTC)
    return now.replace(microsecond=now.microsecond // 1000 * 1000)


def now_env(now: datetime) -> dict[str, str]:
    """`RW_NOW` and `RW_NOW_EPOCH` for the instant `now`."""
    text = format_now(now)
    return {NOW_ENV: text, NOW_EPOCH_ENV: str(int(parse_now(text).timestamp()))}
