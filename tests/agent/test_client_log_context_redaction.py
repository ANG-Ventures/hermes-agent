"""A custom base_url's userinfo password never reaches agent.log via the client lifecycle lines (t_594614e2)."""
import logging
import sys
import types

sys.modules.setdefault("fire", types.SimpleNamespace(Fire=lambda *a, **k: None))
sys.modules.setdefault("firecrawl", types.SimpleNamespace(Firecrawl=object))
sys.modules.setdefault("fal_client", types.SimpleNamespace())

import run_agent

_PW = "s3cr3t-pw-594614e2"
_URL = f"http://u:{_PW}@127.0.0.1:8123/v1?api_key=k3y-594614e2"


class _Client:
    def close(self):
        pass


def _agent():
    agent = run_agent.AIAgent.__new__(run_agent.AIAgent)
    agent.provider = "custom"
    agent.base_url = _URL
    agent.model = "m"
    agent._force_close_tcp_sockets = lambda client: 0
    return agent


def test_client_log_context_drops_userinfo_and_credential_query():
    ctx = _agent()._client_log_context()
    assert _PW not in ctx and "u:" not in ctx and "k3y-594614e2" not in ctx
    assert "base_url=http://127.0.0.1:8123/v1" in ctx


def test_openai_client_closed_record_has_no_password(caplog):
    with caplog.at_level(logging.INFO, logger="run_agent"):
        _agent()._close_openai_client(_Client(), reason="agent_init", shared=True)
    msgs = [r.getMessage() for r in caplog.records if "OpenAI client closed" in r.getMessage()]
    assert msgs, "expected the 'OpenAI client closed' INFO line"
    assert all(_PW not in m and "k3y-594614e2" not in m for m in msgs)
    assert "base_url=http://127.0.0.1:8123/v1" in msgs[0]


def test_model_metadata_info_lines_have_no_password(caplog, monkeypatch):
    from agent import model_metadata as mm

    monkeypatch.setattr(mm, "_query_local_context_length", lambda *a, **k: 64_000)
    monkeypatch.setattr(mm, "_invalidate_cached_context_length", lambda *a, **k: None)
    monkeypatch.setattr(mm, "_maybe_cache_local_context_length", lambda *a, **k: None)
    monkeypatch.setattr(mm, "_resolve_codex_oauth_context_length_with_source", lambda *a, **k: (272_000, "t"))
    with caplog.at_level(logging.INFO, logger=mm.logger.name):
        mm._reconcile_local_cached_context_length("m", _URL, 32_000)  # reconcile line
        monkeypatch.setattr(mm, "_query_local_context_length", lambda *a, **k: 1_000)
        mm._reconcile_local_cached_context_length("m", _URL, 32_000)  # < minimum line
        mm._resolve_custom_codex_route_context_length("m", _URL, "", "custom")  # codex route line
    msgs = [r.getMessage() for r in caplog.records]
    assert any("Reconciling stale local cache entry" in m for m in msgs)
    assert any("Live local probe for" in m for m in msgs)
    assert any("codex_responses route at" in m for m in msgs)
    assert all(_PW not in m and "k3y-594614e2" not in m for m in msgs), msgs
