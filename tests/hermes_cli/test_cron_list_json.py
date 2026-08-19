"""`hermes cron list --json` emits machine-readable job records.

Regression coverage for papercut pc-6b853c88: an agent inspecting a cron job tried
`hermes cron list --json` and `hermes cron runs --job-id`; both flags were unsupported
and the resulting arg error cost a retry. The CLI exposed the box-drawing table only,
so the alternatives were scraping that table or reading `cron/jobs.json` directly
(coupling every caller to the on-disk layout).

`--json` prints the raw job records as a JSON array. The human table is unchanged.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import hermes_cli.cron as cron_cli


JOBS = [
    {
        "id": "67ae66082f41",
        "name": "papercut-weekly-review",
        "schedule_display": "30 9 * * 1",
        "enabled": True,
        "state": "scheduled",
        "next_run_at": "2026-08-24T09:30:00-07:00",
        "repeat": None,      # present-but-null: must not crash (the #32896 class)
        "deliver": None,
        "skills": None,
    },
    {
        "id": "deadbeef0001",
        "name": "disabled-job",
        "schedule_display": "0 3 * * *",
        "enabled": False,
        "state": "paused",
        "next_run_at": None,
    },
]


@pytest.fixture()
def fake_jobs(monkeypatch):
    """Patch the job source that `cron_list` imports at call time."""
    import cron.jobs as jobs_mod

    seen = {}

    def _list_jobs(include_disabled: bool = False):
        seen["include_disabled"] = include_disabled
        return JOBS if include_disabled else [JOBS[0]]

    monkeypatch.setattr(jobs_mod, "list_jobs", _list_jobs)
    return seen


def test_json_output_is_parseable(capsys, fake_jobs):
    cron_cli.cron_list(as_json=True)
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, list)
    assert payload[0]["id"] == "67ae66082f41"
    assert payload[0]["name"] == "papercut-weekly-review"


def test_json_output_has_no_table_chrome(capsys, fake_jobs):
    """The whole point: no box-drawing or ANSI color to scrape past."""
    cron_cli.cron_list(as_json=True)
    out = capsys.readouterr().out
    for chrome in ("┌", "└", "│", "Scheduled Jobs", "\x1b["):
        assert chrome not in out


def test_json_respects_show_all(capsys, fake_jobs):
    cron_cli.cron_list(show_all=True, as_json=True)
    assert fake_jobs["include_disabled"] is True
    assert len(json.loads(capsys.readouterr().out)) == 2


def test_json_default_excludes_disabled(capsys, fake_jobs):
    cron_cli.cron_list(as_json=True)
    assert fake_jobs["include_disabled"] is False
    assert len(json.loads(capsys.readouterr().out)) == 1


def test_json_with_no_jobs_is_an_empty_array(capsys, monkeypatch):
    """An empty result must still be VALID JSON, not the human 'No scheduled jobs.'
    prose — otherwise every caller needs a special case."""
    import cron.jobs as jobs_mod

    monkeypatch.setattr(jobs_mod, "list_jobs", lambda include_disabled=False: [])
    cron_cli.cron_list(as_json=True)
    assert json.loads(capsys.readouterr().out) == []


def test_human_table_is_unchanged_by_default(capsys, fake_jobs):
    """Regression guard: adding --json must not alter the default output."""
    cron_cli.cron_list()
    out = capsys.readouterr().out
    assert "Scheduled Jobs" in out
    assert "67ae66082f41" in out


def test_command_dispatch_passes_the_json_flag(capsys, fake_jobs):
    """Wire-level: `hermes cron list --json` reaches cron_list(as_json=True)."""
    args = SimpleNamespace(cron_command="list", all=False, json=True)
    assert cron_cli.cron_command(args) == 0
    assert isinstance(json.loads(capsys.readouterr().out), list)


def test_command_dispatch_without_the_flag_prints_the_table(capsys, fake_jobs):
    """getattr default keeps older callers (no `json` attr) on the table path."""
    args = SimpleNamespace(cron_command="list", all=False)
    assert cron_cli.cron_command(args) == 0
    assert "Scheduled Jobs" in capsys.readouterr().out


def test_parser_accepts_the_json_flag():
    """The flag must exist on the real parser, not just the handler."""
    import argparse

    from hermes_cli.subcommands.cron import build_cron_parser

    parser = argparse.ArgumentParser()
    build_cron_parser(parser.add_subparsers(dest="command"), cmd_cron=lambda a: 0)
    ns = parser.parse_args(["cron", "list", "--json"])
    assert ns.json is True
    ns_plain = parser.parse_args(["cron", "list"])
    assert ns_plain.json is False
