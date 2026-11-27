"""``start_gateway`` installs the process-wide no-fork spawn hook (hermes-agent#223 wiring).

``tests/tools/test_spawn_nofork_hook.py`` and ``test_spawn_nofork_probe223.py`` exercise
``tools.spawn_nofork`` directly, and ``test_gateway_nofork_lint.py`` ratchets call sites, so all
three stay green if the two lines in ``start_gateway`` that call ``install_process_hook()`` are
deleted. This test drives the real ``start_gateway`` up to the host-attach decision (the first
step that can start anything) with a recorder in place of the hook and stops it there: the hook
must have been installed exactly once, and before that step.
"""

from __future__ import annotations

import asyncio

import pytest

import gateway.code_skew as code_skew
import gateway.run as gateway_run
import tools.spawn_nofork as nofork
import hermes_cli.resource_limits as resource_limits


class _StopBeforeHostAttach(Exception):
    pass


def test_start_gateway_installs_nofork_hook_once_before_host_attach(monkeypatch):
    events: list[str] = []

    async def _host_attach(*_a, **_k):
        events.append("host_attach")
        raise _StopBeforeHostAttach()

    # Only the recorder and the stop are under test; the rest is stubbed so nothing real runs
    # (no os.environ writes, no RLIMIT_NOFILE change, no boot-fingerprint file).
    monkeypatch.setattr(nofork, "install_process_hook", lambda: events.append("install_process_hook"))
    monkeypatch.setattr(gateway_run, "_apply_start_env", lambda: None)
    monkeypatch.setattr(resource_limits, "apply_nofile_soft_limit", lambda *a, **k: None)
    monkeypatch.setattr(code_skew, "record_boot_fingerprint", lambda *a, **k: None)
    monkeypatch.setattr(gateway_run, "_host_attach_or_none", _host_attach)

    with pytest.raises(_StopBeforeHostAttach):
        asyncio.run(gateway_run.start_gateway())

    assert events == ["install_process_hook", "host_attach"], events
