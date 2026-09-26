"""Flake quarantine: neutralize known-flaky tests named in ``CI_TEST_QUARANTINE``.

The merge queue runs HEADGREEN: one red group ejects its PR and rebuilds every
group behind it. A test that fails a merge_group run and then passes on a rerun
(same PR head) costs a whole batch rebuild for nothing. The fleet's
``mq-flake-quarantine`` lane detects that signature and writes the test into the
``CI_TEST_QUARANTINE`` repository variable; ``tests.yml`` exports it to every
slice. This plugin reads it.

Entry shape (JSON list)::

    {"id": "tests/x.py::test_y[param]" | "tests/x.py",
     "card": "t_1234abcd", "owner": "daedalus",
     "expires": "2026-10-03T00:00:00Z", "reason": "..."}

* A node-id entry marks matching tests ``xfail(strict=False)``: they still run
  and still report, but a failure no longer turns the slice red. An entry
  without ``[...]`` covers every parametrization.
* A file entry (no ``::``) skips every test in the file. It exists for the
  per-file wall-clock ceiling, where the harness kills the whole file and an
  xfail mark would not help.
* Never silent: every entry must name ``card`` and ``owner`` and carry an
  ``expires`` timestamp. Entries without them, or past expiry, are ignored,
  so the test runs normally again. The terminal summary lists what was
  applied, what was ignored, and why.

Unset or empty variable means the plugin does nothing.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re

import pytest

ENV_VAR = "CI_TEST_QUARANTINE"
_CARD_RE = re.compile(r"^t_[0-9a-f]{8}$")
_ID_RE = re.compile(r"^tests/\S+\.py(::\S.*)?$")

_applied: dict[str, int] = {}
_ignored: list[str] = []


def _parse_ts(value: str) -> _dt.datetime:
    ts = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        raise ValueError("expires must carry a timezone")
    return ts


def load_entries(raw: str | None, now: _dt.datetime) -> tuple[list[dict], list[str]]:
    """Return (active entries, human-readable reasons for ignored entries)."""
    if not raw or not raw.strip():
        return [], []
    try:
        data = json.loads(raw)
    except ValueError as exc:
        return [], [f"{ENV_VAR} is not valid JSON ({exc}); no test quarantined"]
    if not isinstance(data, list):
        return [], [f"{ENV_VAR} must be a JSON list; no test quarantined"]
    active: list[dict] = []
    ignored: list[str] = []
    for entry in data:
        if not isinstance(entry, dict):
            ignored.append(f"non-object entry {entry!r}")
            continue
        tid = str(entry.get("id") or "")
        card = str(entry.get("card") or "")
        owner = str(entry.get("owner") or "").strip()
        if not _ID_RE.match(tid):
            ignored.append(f"{tid or '<no id>'}: id must be tests/<file>.py[::<test>]")
            continue
        if not _CARD_RE.match(card) or not owner:
            ignored.append(f"{tid}: missing card or owner (a quarantine must name both)")
            continue
        try:
            expires = _parse_ts(str(entry.get("expires") or ""))
        except ValueError:
            ignored.append(f"{tid}: missing or invalid expires")
            continue
        if expires <= now:
            ignored.append(f"{tid}: quarantine expired {entry['expires']} (card {card}); test runs normally")
            continue
        active.append({**entry, "id": tid, "card": card, "owner": owner})
    return active, ignored


def match(nodeid: str, entry_id: str) -> bool:
    """True when ``entry_id`` covers ``nodeid``."""
    if "::" not in entry_id:
        return nodeid.split("::", 1)[0] == entry_id
    if nodeid == entry_id:
        return True
    # "tests/x.py::test_y" covers every "tests/x.py::test_y[...]" parametrization.
    return "[" not in entry_id and nodeid.startswith(entry_id + "[")


def _reason(entry: dict) -> str:
    return (
        f"quarantined flake: card {entry['card']}, owner {entry['owner']}, "
        f"expires {entry['expires']}" + (f" ({entry['reason']})" if entry.get("reason") else "")
    )


def pytest_collection_modifyitems(config, items):  # noqa: D401 - pytest hook
    active, ignored = load_entries(
        os.environ.get(ENV_VAR), _dt.datetime.now(_dt.timezone.utc)
    )
    _ignored.extend(ignored)
    if not active:
        return
    for item in items:
        for entry in active:
            if not match(item.nodeid, entry["id"]):
                continue
            if "::" in entry["id"]:
                item.add_marker(pytest.mark.xfail(reason=_reason(entry), strict=False))
            else:
                item.add_marker(pytest.mark.skip(reason=_reason(entry)))
            _applied[entry["id"]] = _applied.get(entry["id"], 0) + 1
            break


def pytest_terminal_summary(terminalreporter, exitstatus, config):  # noqa: D401
    if not _applied and not _ignored:
        return
    tr = terminalreporter
    tr.section("flake quarantine (CI_TEST_QUARANTINE)")
    for entry_id, n in sorted(_applied.items()):
        tr.write_line(f"applied: {entry_id} ({n} test(s))")
    for why in _ignored:
        tr.write_line(f"ignored: {why}")
