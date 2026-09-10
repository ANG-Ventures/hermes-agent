"""Offline, subprocess-level Codex refresh ownership preflight.

Exit 1 means the proposed shared-root migration is NOT safe. No live credentials
are read: HOME/HERMES_HOME are disposable, imports are pinned to this checkout,
and httpx POST is replaced with a synthetic endpoint. This is a diagnostic, not
an implementation of shared credential ownership.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
from contextlib import nullcontext

ROOT = Path(__file__).resolve().parents[1]
PROVIDER = "openai-codex"


def access(grant):
    payload = {"https://api.openai.com/auth": {"chatgpt_account_id": "same-account"},
               "grant": grant, "exp": 1}
    return "fixture." + base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=") + ".unsigned"


def entry(grant=0):
    return {"id": "fixture-row", "source": "manual:device_code", "auth_type": "oauth",
            "priority": 0, "label": "synthetic", "access_token": access(grant),
            "refresh_token": f"fixture-refresh-{grant}"}


def store(path, row):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "credential_pool": {PROVIDER: [row]}}), encoding="utf-8")


def emit(**row):
    print(json.dumps(row), flush=True)


def worker(home, grant, mode):
    assert Path(os.environ["HOME"]).resolve() in Path(home).resolve().parents
    sys.path.insert(0, str(ROOT))
    sys.meta_path[:] = [f for f in sys.meta_path if "__editable__" not in str(f)]
    import hermes_constants
    import hermes_cli.auth as auth
    import agent.credential_pool as cp
    for module in (hermes_constants, auth, cp):
        assert module.__file__ is not None
        assert Path(module.__file__).resolve().is_relative_to(ROOT)
    assert auth._global_auth_file_path() == Path(os.environ["HOME"]) / ".hermes/auth.json"
    # Defense in depth: no unexpected real network path is permitted.
    import socket
    def denied(*args, **kwargs):
        raise AssertionError("real network forbidden")
    socket.socket.connect = denied

    class Client:
        def __init__(self, *args, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def post(self, url, **kwargs):
            assert url == auth.CODEX_OAUTH_TOKEN_URL
            emit(event="POST", original=kwargs["data"]["refresh_token"] == entry(grant)["refresh_token"])
            assert sys.stdin.readline().strip() == "release"
            if mode == "crash":
                os._exit(73)
            return auth.httpx.Response(200, json={"access_token": access(grant + 100),
                                                 "refresh_token": f"fixture-refresh-{grant + 100}"})
    auth.httpx.Client = Client
    pool = cp.load_pool(PROVIDER)
    original = pool._entries[0]
    assert original.refresh_token == entry(grant)["refresh_token"]
    pool._single_use_refresh_lock_timeout = lambda: 0.4
    if mode == "write-failure":
        def fail_write(*args, **kwargs):
            raise OSError("synthetic write failure")
        auth._save_auth_store = fail_write
    emit(event="READY")
    assert sys.stdin.readline().strip() == "go"
    lock = auth._auth_store_lock(target_path=auth._global_auth_file_path()) if mode == "root-lock-only" else nullcontext()
    try:
        with lock:
            result = pool._refresh_entry(original, force=False)
        emit(event="DONE", returned=bool(result))
    except Exception as exc:
        emit(event="ERROR", kind=type(exc).__name__)


def receive(process):
    if not select.select([process.stdout], [], [], 15)[0]:
        raise AssertionError("worker did not produce an event")
    line = process.stdout.readline()
    assert line, (process.poll(), process.stderr.read())
    return json.loads(line)


def send(process, command):
    process.stdin.write(command + "\n")
    process.stdin.flush()


def run_case(*, inherited=False, same_profile=False, independent=False,
             serialized=False, singleton=False, mode="normal"):
    with tempfile.TemporaryDirectory(prefix="codex-ownership-") as temp:
        home = Path(temp)
        root = home / ".hermes"
        profiles = [root / "profiles" / "a", root / "profiles" / ("a" if same_profile else "b")]
        if inherited:
            store(root / "auth.json", entry())
            if singleton:
                state = json.loads((root / "auth.json").read_text(encoding="utf-8"))
                state["credential_pool"][PROVIDER][0]["source"] = "device_code"
                state["providers"] = {PROVIDER: {"tokens": {
                    "access_token": entry()["access_token"],
                    "refresh_token": entry()["refresh_token"],
                }}}
                (root / "auth.json").write_text(json.dumps(state), encoding="utf-8")
        for i, profile in enumerate(profiles):
            profile.mkdir(parents=True, exist_ok=True)
            if not inherited:
                store(profile / "auth.json", entry(i if independent else 0))
        processes = []
        events = []
        try:
            for i, profile in enumerate(profiles):
                env = dict(os.environ, HOME=str(home), HERMES_HOME=str(profile), PYTHONPATH=str(ROOT))
                env.pop("PYTEST_CURRENT_TEST", None)
                env.pop("HERMES_TEST_HOME", None)
                process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--worker", str(profile),
                                            str(i if independent else 0), mode if i == 0 or mode == "root-lock-only" else "normal"],
                                           cwd=ROOT, env=env, text=True, bufsize=1,
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                processes.append(process)
                assert receive(process) == {"event": "READY"}
            send(processes[0], "go")
            events.append(receive(processes[0]))
            assert events[-1]["event"] == "POST"
            if serialized:
                send(processes[0], "release")
                if mode != "crash":
                    events.append(receive(processes[0]))
                processes[0].wait(timeout=10)
            send(processes[1], "go")
            second = receive(processes[1])
            events.append(second)
            if second["event"] == "POST":
                send(processes[1], "release")
                events.append(receive(processes[1]))
            if not serialized:
                send(processes[0], "release")
                events.append(receive(processes[0]))
            exits = [p.wait(timeout=10) for p in processes]
            assert exits == ([73, 0] if mode == "crash" else [0, 0]), exits
            root_unchanged = None
            if inherited:
                root_unchanged = json.loads((root / "auth.json").read_text(encoding="utf-8"))["credential_pool"][PROVIDER][0]["refresh_token"] == entry()["refresh_token"]
            return {"events": events, "root_unchanged": root_unchanged,
                    "profile_copies": sum((p / "auth.json").exists() for p in set(profiles))}
        finally:
            for p in processes:
                if p.poll() is None:
                    p.kill()
                p.wait(timeout=10)
                for pipe in (p.stdin, p.stdout, p.stderr):
                    pipe.close()


def main():
    cases = {
        "same_profile_lock_control": run_case(same_profile=True),
        "mirrored_profiles": run_case(),
        "inherited_root": run_case(inherited=True),
        "inherited_singleton": run_case(inherited=True, singleton=True),
        "same_profile_serialized_manual": run_case(same_profile=True, serialized=True),
        "root_lock_only_serialized": run_case(inherited=True, serialized=True, mode="root-lock-only"),
        "same_account_independent_grants": run_case(independent=True),
        "crash_after_post": run_case(inherited=True, serialized=True, mode="crash"),
        "write_failure_after_post": run_case(inherited=True, serialized=True, mode="write-failure"),
    }
    checks = {
        "same_profile_serializes": sum(e["event"] == "POST" for e in cases["same_profile_lock_control"]["events"]) == 1,
        "same_profile_manual_waiter_does_not_replay": sum(e["event"] == "POST" and e["original"] for e in cases["same_profile_serialized_manual"]["events"]) == 1,
        "singleton_inheritance_does_not_double_post": sum(e["event"] == "POST" for e in cases["inherited_singleton"]["events"]) == 1,
        "inherited_profiles_do_not_double_post": sum(e["event"] == "POST" for e in cases["inherited_root"]["events"]) == 1,
        "root_lock_only_does_not_replay": sum(e["event"] == "POST" and e["original"] for e in cases["root_lock_only_serialized"]["events"]) == 1,
        "inherited_rotation_commits_to_root": cases["inherited_root"]["root_unchanged"] is False,
        "inherited_rotation_does_not_create_local_shadows": cases["inherited_root"]["profile_copies"] == 0,
        "independent_grants_remain_independent": sum(e["event"] == "POST" and e["original"] for e in cases["same_account_independent_grants"]["events"]) == 2,
        "crash_prevents_replay": sum(e["event"] == "POST" and e["original"] for e in cases["crash_after_post"]["events"]) == 1,
        "failed_write_prevents_replay": sum(e["event"] == "POST" and e["original"] for e in cases["write_failure_after_post"]["events"]) == 1,
    }
    print(json.dumps({"source_root": str(ROOT), "cases": cases, "checks": checks,
                      "migration_safe": all(checks.values())}, indent=2))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        worker(sys.argv[2], int(sys.argv[3]), sys.argv[4])
    else:
        raise SystemExit(main())
