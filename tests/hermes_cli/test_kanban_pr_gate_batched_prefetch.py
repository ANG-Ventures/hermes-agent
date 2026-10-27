"""PR-gate prefetch batches GitHub lookups: one ``gh`` per repo, not per PR.

Context (py-spy --gil, Apollo gateway, 2026-10-26 20:14 PT, 1,737 samples):
9.6 % of ALL samples sat in ``kanban_pr_gate.query_pr``'s
``subprocess.run(["gh", "api", "repos/<r>/pulls/<n>"])``, reached from the
dispatcher tick's ``prefetch_pr_gate_states``. Every five minutes the OPEN/CLOSED
cache expires and the tick forked one ``gh`` per open PR from a multi-GB
process. These guards pin:

* call count: N cache-miss PRs in one repo -> 1 ``gh api graphql`` call per 50,
  one per repo (fork/main: one REST call per PR);
* normalization parity with ``query_pr``'s REST shape;
* the "cannot tell -> None, never cached" failure contract, including gh's
  rc=1-with-partial-data behaviour for a missing PR;
* cache/TTL parity: MERGED forever, OPEN/CLOSED re-queried after 300 s.

``subprocess.run`` is faked at the process boundary, so the REAL ``query_pr``
oracle is in play (no test seam installed) and the sandbox guard is satisfied.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_pr_gate as prg

_REAL_RUN = subprocess.run
_ALIAS_RE = re.compile(r"p(\d+): pullRequest\(number: (\d+)\)")
_AFTER_ANY_BLOCK = "2099-01-01T00:00:00Z"


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_SANDBOX", "1")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture(autouse=True)
def _clear_pr_cache(monkeypatch: pytest.MonkeyPatch):
    prg.clear_cache()
    monkeypatch.setattr(prg, "repo_exists", lambda slug: True, raising=False)
    yield
    prg.clear_cache()


class FakeGh:
    """Fake ``gh`` at ``subprocess.run``: canned PR states for REST and GraphQL.

    ``states[(repo, n)]`` is ``"OPEN" | "CLOSED" | "MERGED"`` or absent
    (missing PR). ``mode`` forces a whole-call failure.
    """

    def __init__(self, states: dict, mode: str = "ok") -> None:
        self.states = {(r.lower(), n): s for (r, n), s in states.items()}
        self.mode = mode
        self.calls: list[list[str]] = []

    def gql_node(self, repo: str, n: int):
        state = self.states.get((repo.lower(), n))
        if state is None:
            return None
        merged = state == "MERGED"
        return {
            "state": state,
            "mergedAt": _AFTER_ANY_BLOCK if merged else None,
            "mergeCommit": {"oid": f"sha{n:037d}"} if merged else None,
        }

    def rest_payload(self, repo: str, n: int):
        state = self.states.get((repo.lower(), n))
        if state is None:
            return None
        merged = state == "MERGED"
        return {
            "state": "closed" if state != "OPEN" else "open",
            "merged_at": _AFTER_ANY_BLOCK if merged else None,
            "merge_commit_sha": f"sha{n:037d}" if merged else None,
        }

    def _done(self, args, rc: int, out: str):
        return subprocess.CompletedProcess(args, rc, stdout=out, stderr="")

    def __call__(self, args, *a, **kw):
        if not (isinstance(args, list) and args[:2] == ["gh", "api"]):
            return _REAL_RUN(args, *a, **kw)
        self.calls.append(list(args))
        if self.mode == "timeout":
            raise subprocess.TimeoutExpired(args, kw.get("timeout") or 1)
        if self.mode == "rc":
            return self._done(args, 1, "")
        if self.mode == "junk":
            return self._done(args, 0, "<html>502 Bad Gateway</html>")
        if self.mode == "errors_only":
            return self._done(
                args, 1,
                json.dumps({"errors": [{"type": "RATE_LIMITED",
                                        "message": "API rate limit exceeded"}]}),
            )
        if args[2] == "graphql":
            fields = dict(
                x.split("=", 1) for x in args[3:] if not x.startswith("-")
            )
            repo = f"{fields['owner']}/{fields['name']}"
            repository, errors = {}, []
            for alias, num in _ALIAS_RE.findall(fields["query"]):
                node = self.gql_node(repo, int(num))
                repository[f"p{alias}"] = node
                if node is None:
                    errors.append({"type": "NOT_FOUND", "path": ["repository", f"p{alias}"]})
            body = {"data": {"repository": repository}}
            if errors:
                body["errors"] = errors
            # gh exits 1 whenever ``errors`` is present, data or not.
            return self._done(args, 1 if errors else 0, json.dumps(body))
        m = re.fullmatch(r"repos/(.+)/pulls/(\d+)", args[2])
        if m:
            payload = self.rest_payload(m.group(1), int(m.group(2)))
            if payload is None:
                return self._done(args, 1, json.dumps({"message": "Not Found"}))
            return self._done(args, 0, json.dumps(payload))
        return self._done(args, 1, "")


def _install(monkeypatch, fake: FakeGh) -> FakeGh:
    monkeypatch.setattr(prg.subprocess, "run", fake)
    return fake


def _blocked_card(conn, refs: list[str]) -> str:
    tid = kb.create_task(conn, title="gated card", body=None)
    kb.claim_task(conn, tid)
    assert kb.block_task(
        conn, tid, reason="waits on " + " ".join(refs) + " then unblock me",
        kind="needs_input",
        expected_run_id=kb.get_task(conn, tid).current_run_id,
    )
    return tid


def _cards(conn, repo: str, numbers, per_card: int = 10) -> None:
    numbers = list(numbers)
    for i in range(0, len(numbers), per_card):
        _blocked_card(conn, [f"{repo}#{n}" for n in numbers[i:i + per_card]])


def _keys(repo: str, numbers) -> list[tuple[str, int]]:
    return [(repo.lower(), n) for n in numbers]


# ---------------------------------------------------------------------------
# Call count
# ---------------------------------------------------------------------------


def test_forty_misses_in_one_repo_cost_one_gh_call(kanban_home, monkeypatch) -> None:
    fake = _install(monkeypatch, FakeGh({("o/r", n): "OPEN" for n in range(1, 41)}))
    with kb.connect() as conn:
        _cards(conn, "o/r", range(1, 41))
        result = prg.prefetch_pr_gate_states(conn, max_lookups=500, now=1000.0)
    assert len(fake.calls) == 1, f"{len(fake.calls)} gh calls for 40 PRs"
    assert fake.calls[0][:3] == ["gh", "api", "graphql"]
    assert all(result.payloads[k]["state"] == "OPEN" for k in _keys("o/r", range(1, 41)))


def test_two_repos_cost_two_gh_calls(kanban_home, monkeypatch) -> None:
    states = {("o/r", n): "OPEN" for n in range(1, 21)}
    states.update({("b/one", n): "MERGED" for n in range(1, 21)})
    fake = _install(monkeypatch, FakeGh(states))
    with kb.connect() as conn:
        _cards(conn, "o/r", range(1, 21))
        _cards(conn, "b/one", range(1, 21))
        result = prg.prefetch_pr_gate_states(conn, max_lookups=500, now=1000.0)
    assert len(fake.calls) == 2
    assert result.payloads[("b/one", 7)]["state"] == "MERGED"
    assert result.payloads[("o/r", 7)]["state"] == "OPEN"


def test_chunking_120_prs_cost_three_calls(kanban_home, monkeypatch) -> None:
    fake = _install(monkeypatch, FakeGh({("o/r", n): "OPEN" for n in range(1, 121)}))
    with kb.connect() as conn:
        _cards(conn, "o/r", range(1, 121))
        result = prg.prefetch_pr_gate_states(conn, max_lookups=500, now=1000.0)
    assert len(fake.calls) == 3
    sizes = sorted(len(_ALIAS_RE.findall(c[-1])) for c in fake.calls)
    assert sizes == [20, 50, 50]
    assert sum(1 for v in result.payloads.values() if v) == 120


def test_per_tick_lookup_cap_still_applies(kanban_home, monkeypatch) -> None:
    fake = _install(monkeypatch, FakeGh({("o/r", n): "OPEN" for n in range(1, 41)}))
    with kb.connect() as conn:
        _cards(conn, "o/r", range(1, 41))
        result = prg.prefetch_pr_gate_states(conn, now=1000.0)
    assert len(fake.calls) == 1
    assert len(result.payloads) == prg.MAX_LOOKUPS_PER_TICK
    assert len(result.capped) == 40 - prg.MAX_LOOKUPS_PER_TICK


# ---------------------------------------------------------------------------
# Normalization parity with query_pr (REST)
# ---------------------------------------------------------------------------


def test_batch_normalization_matches_query_pr(monkeypatch) -> None:
    states = {("o/r", 1): "OPEN", ("o/r", 2): "CLOSED", ("o/r", 3): "MERGED"}
    _install(monkeypatch, FakeGh(states))
    query_prs = getattr(prg, "query_prs", None)
    assert query_prs is not None, "no batched lookup (query_prs) on this tree"
    batch = query_prs("o/r", [1, 2, 3, 4])
    for n in (1, 2, 3, 4):
        assert batch[n] == prg.query_pr("o/r", n), n
    assert batch[3] == {
        "state": "MERGED", "mergedAt": _AFTER_ANY_BLOCK,
        "mergeCommitSha": f"sha{3:037d}",
    }
    assert batch[4] is None


# ---------------------------------------------------------------------------
# Failure contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["rc", "timeout", "junk", "errors_only"])
def test_whole_call_failure_is_none_for_all_and_never_cached(
    kanban_home, monkeypatch, mode,
) -> None:
    fake = _install(
        monkeypatch, FakeGh({("o/r", n): "MERGED" for n in range(1, 6)}, mode=mode),
    )
    with kb.connect() as conn:
        _cards(conn, "o/r", range(1, 6))
        result = prg.prefetch_pr_gate_states(conn, max_lookups=500, now=1000.0)
        assert all(result.payloads[k] is None for k in _keys("o/r", range(1, 6)))
        outcomes = prg.reevaluate_pr_gates(conn, prefetched=result)
    assert len(fake.calls) == 1
    assert prg._CACHE == {}
    assert all(o.action != "unblocked" for o in outcomes)


def test_partial_errors_use_resolved_nodes(kanban_home, monkeypatch) -> None:
    # #2 is missing: gh prints data for #1/#3 plus a NOT_FOUND error, rc=1.
    fake = _install(monkeypatch, FakeGh({("o/r", 1): "OPEN", ("o/r", 3): "CLOSED"}))
    with kb.connect() as conn:
        # One ref per card: the locked pass stops a card at its first
        # unresolvable ref, so co-carding them would hide #3 from the cache.
        _cards(conn, "o/r", [1, 2, 3], per_card=1)
        result = prg.prefetch_pr_gate_states(conn, max_lookups=500, now=1000.0)
        prg.reevaluate_pr_gates(conn, prefetched=result)
    assert len(fake.calls) == 1
    assert result.payloads[("o/r", 2)] is None
    assert result.payloads[("o/r", 3)]["state"] == "CLOSED"
    assert set(prg._CACHE) == {("o/r", 1), ("o/r", 3)}
    assert prg._CACHE[("o/r", 1)].state == "OPEN"
    assert prg._CACHE[("o/r", 3)].state == "CLOSED"


# ---------------------------------------------------------------------------
# Cache / TTL parity
# ---------------------------------------------------------------------------


def _numbers_queried(fake: FakeGh) -> list[int]:
    out: list[int] = []
    for call in fake.calls:
        if call[2] == "graphql":
            out += [int(n) for _, n in _ALIAS_RE.findall(call[-1])]
        else:
            out.append(int(call[2].rsplit("/", 1)[1]))
    return sorted(out)


def test_merged_cached_forever_open_requeried_after_ttl(kanban_home, monkeypatch) -> None:
    fake = _install(
        monkeypatch,
        FakeGh({("o/r", 1): "MERGED", ("o/r", 2): "OPEN", ("o/r", 3): "CLOSED"}),
    )
    t0 = 1000.0
    with kb.connect() as conn:
        # MERGED #1 rides with OPEN #2 so the card stays blocked (and #1 stays
        # a candidate); #4 is missing -> None, never cached.
        _blocked_card(conn, ["o/r#1", "o/r#2"])
        _blocked_card(conn, ["o/r#3"])
        _blocked_card(conn, ["o/r#4"])
        first = prg.prefetch_pr_gate_states(conn, max_lookups=500, now=t0)
        prg.reevaluate_pr_gates(conn, prefetched=first)
        assert set(prg._CACHE) == {("o/r", 1), ("o/r", 2), ("o/r", 3)}
        assert prg._CACHE[("o/r", 1)].terminal
        assert prg._CACHE[("o/r", 1)].sha == f"sha{1:037d}"
        assert _numbers_queried(fake) == [1, 2, 3, 4]

        fake.calls.clear()
        within = prg.prefetch_pr_gate_states(
            conn, max_lookups=500, now=t0 + prg.CACHE_TTL_SECONDS - 1,
        )
        # Only the uncached None is retried inside the TTL.
        assert _numbers_queried(fake) == [4]
        prg.reevaluate_pr_gates(conn, prefetched=within)

        fake.calls.clear()
        prg.prefetch_pr_gate_states(
            conn, max_lookups=500, now=t0 + prg.CACHE_TTL_SECONDS + 1,
        )
        # OPEN/CLOSED expired; MERGED is permanent.
        assert _numbers_queried(fake) == [2, 3, 4]
        assert len(fake.calls) == 1


def test_injected_query_seam_still_used_per_pr(kanban_home, monkeypatch) -> None:
    fake = _install(monkeypatch, FakeGh({}))
    seen: list[tuple[str, int]] = []

    def seam(repo, number):
        seen.append((repo, number))
        return {"state": "OPEN", "mergedAt": None, "mergeCommitSha": None}

    with kb.connect() as conn:
        _cards(conn, "o/r", [1, 2, 3])
        result = prg.prefetch_pr_gate_states(
            conn, query_fn=seam, max_lookups=500, now=1000.0,
        )
    assert sorted(seen) == [("o/r", 1), ("o/r", 2), ("o/r", 3)]
    assert fake.calls == []
    assert result.payloads[("o/r", 2)]["state"] == "OPEN"
