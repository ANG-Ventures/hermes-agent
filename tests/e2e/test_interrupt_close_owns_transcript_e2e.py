"""E2E for fork#1101 (t_f40dc54a / t_9a34acc6): interrupt mid-tool-call, restart,
next user turn -- a provider that owns its transcript must NOT be sent the
harness-authored interrupt-close row; every other provider still is.

Real path, no mocks inside the agent:

* a stub OpenAI-shape bridge (``_stub_openai_bridge``) records every request
  body the harness puts on the wire;
* a throwaway ``HERMES_HOME`` holds a user model-provider plugin registering
  the lane (``owns_transcript=True`` or the default), discovered by the real
  ``providers`` registry;
* phase 1 runs in its own interpreter: real ``AIAgent`` + ``SessionDB``, the
  stub answers with a ``terminal`` tool call, and while the tool runs the agent
  gets ``request_hard_interrupt`` -- the call the gateway makes on every running
  agent when it restarts -- so ``close_interrupted_tool_sequence`` persists the
  close row;
* phase 2 is a fresh interpreter (the restart): the transcript is reloaded the
  way ``SessionStore.load_transcript`` does and the next user turn is sent.

Assertions (card t_9a34acc6):
  (A) owns_transcript lane: no close row / "Operation interrupted" on the wire,
      roles go ``tool -> user``;
  (B) control lane: the close row IS sent (strict alternation, #48879);
  (C) state.db holds the close row in both lanes.

Red-pre-fix control: with the omit branch in ``conversation_loop`` removed
(or ``provider_owns_transcript`` forced False) lane (A) fails -- see
``test_omit_branch_is_what_makes_lane_a_pass``.

Set ``OWNS_TRANSCRIPT_E2E_EVIDENCE=<dir>`` to dump the stub's recorded bodies.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tests.e2e._stub_openai_bridge import StubBridge

REPO = Path(__file__).resolve().parents[2]
DRIVER = Path(__file__).with_name("_owns_transcript_driver.py")
CLOSE_TEXT = "Operation interrupted"


def _write_provider(home: Path, name: str, base_url: str, owns: bool) -> None:
    pdir = home / "plugins" / "model-providers" / name
    pdir.mkdir(parents=True)
    (pdir / "plugin.yaml").write_text(
        f"name: {name}-provider\nkind: model-provider\nversion: 0.0.1\ndescription: e2e stub\n"
    )
    (pdir / "__init__.py").write_text(textwrap.dedent(f"""
        from providers import register_provider
        from providers.base import ProviderProfile

        register_provider(ProviderProfile(
            name={name!r},
            api_mode="chat_completions",
            env_vars=("STUB_BRIDGE_KEY",),
            base_url={base_url!r},
            auth_type="api_key",
            owns_transcript={owns!r},
            fallback_models=("stub-model",),
        ))
    """))


def _run_lane(tmp_path: Path, owns: bool, *, sabotage_omit: bool = False) -> dict:
    stub = StubBridge().start()
    try:
        home = tmp_path / "home"
        work = tmp_path / "work"
        home.mkdir()
        work.mkdir()
        provider = "stub-owns" if owns else "stub-plain"
        _write_provider(home, provider, stub.base_url, owns)
        (home / "config.yaml").write_text(
            f"model:\n  default: stub-model\n  provider: {provider}\n"
            f"terminal:\n  cwd: {work}\n"
        )
        env = {
            k: v for k, v in os.environ.items()
            if not k.startswith(("HERMES_", "PYTEST_")) and k not in ("TERMINAL_CWD",)
        }
        env.update({
            "HERMES_HOME": str(home),
            "TERMINAL_CWD": str(work),
            "STUB_BRIDGE_KEY": "stub-key",
            "PYTHONPATH": str(REPO),
        })
        if sabotage_omit:
            # Red-pre-fix control: force the owns_transcript lookup False in
            # the driver process -- equivalent to the pre-#1101 send path.
            env["OWNS_TRANSCRIPT_E2E_SABOTAGE"] = "1"
        sid = "e2e_owns_" + ("on" if owns else "off")
        marker = work / "tool_started"

        def phase(name: str) -> dict:
            proc = subprocess.run(
                [sys.executable, str(DRIVER), name, provider, stub.base_url, sid, str(marker)],
                cwd=str(work), env=env, capture_output=True, text=True, timeout=180,
            )
            assert proc.returncode == 0, (
                f"phase {name} rc={proc.returncode}\nstdout={proc.stdout[-2000:]}\n"
                f"stderr={proc.stderr[-4000:]}"
            )
            return json.loads(proc.stdout.strip().splitlines()[-1])

        p1 = phase("interrupt")
        n_phase1 = len(stub.snapshot())
        p2 = phase("resume")
        bodies = stub.snapshot()

        con = sqlite3.connect(home / "state.db")
        db_rows = con.execute(
            "select role, content, finish_reason from messages "
            "where session_id=? order by id", (sid,)
        ).fetchall()
        con.close()
    finally:
        stub.stop()

    # Main-loop requests carry the tool schema; auxiliary calls (title
    # generation) do not and are not part of the conversation wire.
    resume_bodies = [b for b in bodies[n_phase1:] if b.get("tools")]
    out = {
        "provider": provider, "phase1": p1, "phase2": p2,
        "all_bodies": bodies, "resume_bodies": resume_bodies, "db_rows": db_rows,
    }
    ev = os.environ.get("OWNS_TRANSCRIPT_E2E_EVIDENCE")
    if ev:
        Path(ev).mkdir(parents=True, exist_ok=True)
        tag = provider + ("-sabotaged" if sabotage_omit else "")
        Path(ev, f"{tag}.json").write_text(json.dumps(out, indent=2, default=str))
    return out


def _convo(body: dict) -> list[dict]:
    return [m for m in body.get("messages", []) if m.get("role") != "system"]


def _has_close_row(msgs: list[dict]) -> bool:
    return any(
        m.get("role") == "assistant"
        and (CLOSE_TEXT in str(m.get("content") or "")
             or m.get("finish_reason") == "interrupt_close")
        for m in msgs
    )


def _db_has_close_row(rows) -> bool:
    return any(r[0] == "assistant" and r[2] == "interrupt_close" for r in rows)


def _assert_phase1_closed(lane: dict) -> None:
    assert lane["phase1"] == {"interrupted": True}, lane["phase1"]
    # (C) persisted state.db holds the close row
    assert _db_has_close_row(lane["db_rows"]), lane["db_rows"]
    assert lane["resume_bodies"], "resume turn never reached the stub"


def test_owns_transcript_lane_omits_close_row_on_the_wire(tmp_path):
    lane = _run_lane(tmp_path, owns=True)
    _assert_phase1_closed(lane)
    first = _convo(lane["resume_bodies"][0])
    # (A) no close row reaches the wire
    assert not _has_close_row(first), json.dumps(first, indent=1)
    for body in lane["resume_bodies"]:
        assert not _has_close_row(_convo(body))
    # roles go tool -> user
    roles = [m["role"] for m in first]
    assert roles == ["user", "assistant", "tool", "user"], roles
    assert first[-1]["content"] == "status?" or "status?" in str(first[-1]["content"])


def test_control_lane_still_sends_close_row(tmp_path):
    lane = _run_lane(tmp_path, owns=False)
    _assert_phase1_closed(lane)
    first = _convo(lane["resume_bodies"][0])
    # (B) strict-alternation providers get the close row (#48879)
    assert _has_close_row(first), json.dumps(first, indent=1)
    roles = [m["role"] for m in first]
    assert roles == ["user", "assistant", "tool", "assistant", "user"], roles


def test_omit_branch_is_what_makes_lane_a_pass(tmp_path):
    """Negative control: same owns_transcript lane with the omit branch
    disabled (pre-#1101 behaviour) puts the close row on the wire -- proving
    lane (A)'s pass comes from the fix, not from the rig."""
    lane = _run_lane(tmp_path, owns=True, sabotage_omit=True)
    _assert_phase1_closed(lane)
    assert _has_close_row(_convo(lane["resume_bodies"][0]))
