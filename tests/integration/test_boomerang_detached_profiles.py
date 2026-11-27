"""Two actual detached children resolve fresh per-home credentials and safe endpoints."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import pytest
from contextlib import ExitStack
from types import SimpleNamespace

from tools.delegate_tool_routing import persist_route
from tools.delegate_detached import SCHEMA_VERSION
from tests.integration.test_boomerang_parent_routing_http import endpoints


@pytest.mark.parametrize("routes", ["provider", "direct", "explicit", "mixed-parent-first", "mixed-direct-first"])
def test_detached_two_profile_process_isolation(tmp_path, routes):
    processes = []
    with ExitStack() as stack:
        cases = []
        for name in ("a", "b"):
            sentinel = "DETACHED-" + name
            base, trace = stack.enter_context(endpoints(sentinel))
            home = tmp_path / name
            home.mkdir()
            key = "SYNTHETIC-SECRET-" + name
            (home / ".env").write_text("OPENAI_API_KEY=" + key + "\n")
            # Keep an endpoint-bound credential reference while unrelated defaults change.
            direct_key = "SYNTHETIC-DIRECT-" + name
            (home / "config.yaml").write_text("model:\n  provider: openai\n  default: wrong-current-default\ncustom_providers:\n- name: selected\n  base_url: " + base + "/cross-vendor/v1\n  api_key_env: OPENAI_API_KEY\n  api_mode: chat_completions\ndelegation:\n  base_url: " + base + "/cross-vendor/v1\n  api_key: " + direct_key + "\n  model: wrong-worker-default\n  fallback_providers: []\n")
            p = SimpleNamespace(model="selected-" + name, provider="custom", requested_provider="custom:selected",
                api_mode="chat_completions", base_url=base + "/cross-vendor/v1", api_key=key,
                request_overrides={}, _fallback_chain=[])
            route = persist_route(p)
            record = {"execution": {"model": p.model, "provider": "openai", "api_mode": p.api_mode,
                "fallback_chain": [], "max_iterations": 4, "toolsets": [], "parent_toolsets": [],
                "credential_ref": {"source": "provider", "provider": "custom:selected", "inherited_route": True}},
                "route": {"parent_session_id": "20261108_000000_" + name, "platform": "discord"},
                "source": {"tasks": [{"goal": "Return the response verbatim", "inherit_mode": "summary",
                    "route_source": "live-parent", "parent_route": route, "materialized_prefill_messages": []}]}}
            direct_task = {"goal": "Return the response verbatim", "route_source": "delegation-default",
                "credential_ref": {"source": "delegation_config"}, "materialized_prefill_messages": [],
                "parent_route": {**route, "provider": "custom", "model": "direct-" + name}}
            if routes in ("direct", "explicit"):
                if routes == "explicit":
                    direct_task["route_source"] = "explicit-child"
                record["source"]["tasks"] = [direct_task]
                record["execution"].update(model="direct-" + name, base_url=p.base_url,
                    credential_ref={"source": "delegation_config"})
            elif routes.startswith("mixed"):
                record["source"]["tasks"].append(direct_task)
                if routes == "mixed-direct-first":
                    record["source"]["tasks"].reverse()
                    record["execution"].update(model="direct-" + name, base_url=p.base_url,
                        credential_ref={"source": "delegation_config"})
            job = home / "job"
            job.mkdir()
            payload = json.dumps({"schema_version": SCHEMA_VERSION, "record": record})
            assert key not in payload and direct_key not in payload
            (job / "spec.json").write_text(payload)
            env = {k: v for k, v in os.environ.items() if not k.startswith(("HERMES_", "OPENAI_", "OPENROUTER_", "ANTHROPIC_"))}
            env.update(HERMES_HOME=str(home), HERMES_TEST_ISOLATION="1", HERMES_PROFILE_NAME=name,
                       TOKIO_WORKER_THREADS="2", PYTHONPATH=str(Path(__file__).resolve().parents[2]))
            log = stack.enter_context((job / "child.log").open("w"))
            proc = subprocess.Popen([sys.executable, str(Path(__file__).parent / "fixtures/boomerang_detached_child.py"), str(job)],
                                    env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            processes.append(proc)
            def reap(proc=proc):
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=3)
            stack.callback(reap)
            cases.append((name, sentinel, key, trace, job, proc))
        try:
            for name, sentinel, key, trace, job, proc in cases:
                assert proc.wait(timeout=30) == 0, (job / "child.log").read_text()
                result = json.loads((job / "result.json").read_text())
                assert result["results"][0]["status"] == "completed", result
                assert result["results"][0]["summary"] == sentinel, result
                expected = {"selected-" + name: "Bearer " + key} if routes == "provider" else {
                    "direct-" + name: "Bearer SYNTHETIC-DIRECT-" + name}
                if routes.startswith("mixed"):
                    expected["selected-" + name] = "Bearer " + key
                assert len(trace) == len(expected), trace
                assert {row["model"]: row["auth"] for row in trace} == expected
                assert all(item["status"] == "completed" and item["summary"] == sentinel
                           for item in result["results"]), result
                for path in (job / "spec.json", job / "result.json", job / "child.log"):
                    assert key not in path.read_text() and "SYNTHETIC-DIRECT-" not in path.read_text(), path
                print(json.dumps({"profile": name, "pid": proc.pid, "routes": routes,
                    "trace": [{k: v for k, v in row.items() if k != "auth"} for row in trace],
                    "auth_matches_endpoint_reference": True, "sentinel": sentinel,
                    "status": result["results"][0]["status"]}))
        finally:
            for proc in processes:
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=3)


def test_launch_failure_reaps_already_started_child(tmp_path, monkeypatch):
    real_popen = subprocess.Popen
    started = []
    def launch(*args, **kwargs):
        if started:
            raise RuntimeError("injected second launch failure")
        proc = real_popen(*args, **kwargs)
        started.append(proc)
        return proc
    monkeypatch.setattr(subprocess, "Popen", launch)
    with pytest.raises(RuntimeError, match="second launch failure"):
        test_detached_two_profile_process_isolation(tmp_path, "provider")
    assert len(started) == 1
    assert started[0].poll() is not None
