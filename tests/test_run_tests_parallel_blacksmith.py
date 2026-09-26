"""Blacksmith = the paid third rung of the slice venue ladder (t_6804be8c).

Order: free self-hosted -> free GitHub-hosted -> paid Blacksmith. The last N
GitHub-hosted slices move to Blacksmith with their arch kept, and only for
trusted events (push, merge_group, same-repo pull_request). Exercised through
the same CLI entry point and workflow binding CI uses.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/run_tests_parallel.py"
POOL = '["self-hosted","hermes-ci"]'
HOSTED = '["ubuntu-latest"]'
ARM = '["ubuntu-24.04-arm"]'
BS = '["blacksmith-4vcpu-ubuntu-2404"]'
BS_ARM = '["blacksmith-4vcpu-ubuntu-2404-arm"]'
# core smoke first (lightest), then 7 non-core slices with distinct weights.
WEIGHTS8 = (0.1, 40, 10, 30, 5, 20, 1, 15)


def generate(monkeypatch, capsys, *, slots=2, arm="0", x64_min="2", bs=None,
             event="merge_group", same_repo=None):
    spec = importlib.util.spec_from_file_location("bs_runner", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    files = [ROOT / mod._CORE_SMOKE_TESTS[0]] + [ROOT / f"tests/test_f{i}.py" for i in range(1, len(WEIGHTS8))]
    monkeypatch.setattr(mod, "_discover_files", lambda roots: files)
    monkeypatch.setattr(mod, "_compute_lpt_slices", lambda *a: [[f] for f in files])
    monkeypatch.setattr(mod, "_load_durations", lambda root: {mod._format_file(f, ROOT): d for f, d in zip(files, WEIGHTS8)})
    args = [str(SCRIPT), "--generate-slices", str(len(files)), "--self-hosted-labels", POOL,
            "--self-hosted-slots", str(slots), f"--arm-hosted-slices={arm}", f"--x64-hosted-min={x64_min}"]
    if bs is not None:
        args.append(f"--blacksmith-slices={bs}")
    if event is not None:
        args.append(f"--event={event}")
    if same_repo is not None:
        args.append(f"--same-repo={same_repo}")
    monkeypatch.setattr(sys, "argv", args)
    assert mod.main() == 0
    return [s["runs_on"] for s in json.loads(capsys.readouterr().out)["slice"]]


@pytest.mark.parametrize("bs", [None, "", "0", "-1", "oops", "1.5"])
def test_unset_zero_or_invalid_count_is_a_no_op(monkeypatch, capsys, bs):
    assert generate(monkeypatch, capsys, bs=bs) == generate(monkeypatch, capsys, bs=None)
    assert BS not in generate(monkeypatch, capsys, bs=bs)


@pytest.mark.parametrize("n", range(0, 10))
def test_ladder_takes_the_last_n_hosted_slices_and_never_the_pool(monkeypatch, capsys, n):
    runs_on = generate(monkeypatch, capsys, slots=3, bs=str(n))
    # Rung 1 untouched whatever N is.
    assert runs_on[:3] == [POOL] * 3
    hosted = runs_on[3:]
    moved = min(n, len(hosted))
    # Rung 3 is exactly the tail of rung 2.
    assert hosted == [HOSTED] * (len(hosted) - moved) + [BS] * moved


def test_arch_is_preserved_under_arm_first_routing(monkeypatch, capsys):
    base = generate(monkeypatch, capsys, slots=2, arm="3")
    assert base == [POOL, POOL, ARM, ARM, HOSTED, ARM, HOSTED, ARM]
    runs_on = generate(monkeypatch, capsys, slots=2, arm="3", bs="3")
    # Last three hosted slices (5, 6, 7 zero-based) move, each keeping its arch.
    assert runs_on == [POOL, POOL, ARM, ARM, HOSTED, BS_ARM, BS, BS_ARM]
    # x64 canary count survives: every x64 slice is still x64 (hosted or Blacksmith).
    assert runs_on.count(HOSTED) + runs_on.count(BS) == base.count(HOSTED)


@pytest.mark.parametrize("event,same_repo", [
    ("push", None), ("merge_group", None), ("pull_request", "true"), ("pull_request", "True"),
])
def test_trusted_events_route(monkeypatch, capsys, event, same_repo):
    runs_on = generate(monkeypatch, capsys, bs="2", event=event, same_repo=same_repo)
    assert runs_on.count(BS) == 2


@pytest.mark.parametrize("event,same_repo", [
    ("pull_request", "false"),          # fork PR
    ("pull_request", ""),               # unknown head repo
    ("pull_request", None),
    ("pull_request_target", "true"),    # privileged fork-reachable event
    ("workflow_dispatch", "true"),
    ("schedule", None),
    ("", "true"),
    (None, None),                        # generator called without an event
])
def test_trust_guard_fails_closed(monkeypatch, capsys, event, same_repo):
    runs_on = generate(monkeypatch, capsys, bs="5", event=event, same_repo=same_repo)
    assert not any("blacksmith" in r for r in runs_on)
    assert runs_on == generate(monkeypatch, capsys, bs=None)


def test_scoped_plugin_matrix_uses_the_same_rung(monkeypatch, capsys):
    import subprocess
    plugin = next(p.name for p in sorted((ROOT / "tests/plugins").iterdir()) if p.is_dir() and any(p.glob("test_*.py")))
    base = [sys.executable, str(SCRIPT), "--generate-slices", "4", "--test-scope", f"plugin:{plugin}",
            "--self-hosted-labels", POOL, "--self-hosted-slots", "1", "--blacksmith-slices", "9"]
    trusted = subprocess.run(base + ["--event", "push"], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)
    fork = subprocess.run(base + ["--event", "pull_request", "--same-repo", "false"], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)
    assert trusted.returncode == 0 and fork.returncode == 0, trusted.stderr + fork.stderr
    t = {s["name"]: s["runs_on"] for s in json.loads(trusted.stdout)["slice"]}
    f = {s["name"]: s["runs_on"] for s in json.loads(fork.stdout)["slice"]}
    assert t[f"plugin {plugin}"] == POOL and t["core smoke"] == POOL
    assert f == {k: (HOSTED if v == BS else v) for k, v in t.items()}
    assert "blacksmith" not in fork.stdout


def test_workflow_binds_variable_event_and_same_repo():
    jobs = yaml.safe_load((ROOT / ".github/workflows/tests.yml").read_text())["jobs"]
    step = next(s for s in jobs["generate"]["steps"] if s.get("id") == "matrix")
    env = step["env"]
    assert env["CI_BLACKSMITH_SLICES"] == "${{ vars.CI_BLACKSMITH_SLICES }}"
    assert env["EVENT_NAME"] == "${{ github.event_name }}"
    assert env["SAME_REPO"] == "${{ github.event.pull_request.head.repo.full_name == github.repository }}"
    for flag in ('--blacksmith-slices="$CI_BLACKSMITH_SLICES"', '--event="$EVENT_NAME"', '--same-repo="$SAME_REPO"'):
        assert flag in step["run"]
    # Membership never changes: every slice still takes its label from the matrix.
    assert jobs["test"]["runs-on"] == "${{ fromJSON(matrix.slice.runs_on) }}"


def test_blacksmith_is_reachable_only_through_tests_yml():
    """The host controller and spend monitor (hermes-home scripts/ci-placement.py
    BS_WORKFLOWS) scan only ci.yaml runs for Blacksmith minutes, because tests.yml
    (called from ci.yaml) is the one place a blacksmith label can come from. A new use
    site must add its caller workflow to BS_WORKFLOWS, or spend goes uncounted."""
    wf = ROOT / ".github/workflows"
    users = sorted(p.name for p in wf.glob("*.y*ml")
                   if "blacksmith" in p.read_text(encoding="utf-8").lower())
    assert users == ["tests.yml"]
    callers = sorted(p.name for p in wf.glob("*.y*ml")
                     if "./.github/workflows/tests.yml" in p.read_text(encoding="utf-8"))
    assert callers == ["ci.yaml"]
