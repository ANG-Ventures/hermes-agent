"""``switch_model(probe_catalog=False)`` opens no socket on a COLD models.dev cache.

FleetReview #970 (C3 re-bucket, t_7da6cadf): the sibling arm in
test_no_network_reachable_from_loop.py stubs ``fetch_models_dev`` out, so the
cold-cache path (no memory or disk cache: stage 4, a singleflight FOREGROUND
fetch) was never exercised. That path is reachable from the event loop on a
fresh install or after the cache file is removed. Here the real
``fetch_models_dev`` runs against an empty cache and every connect raises.
"""
from __future__ import annotations

import socket


def test_switch_model_probe_off_cold_models_dev_cache_opens_no_socket(monkeypatch, tmp_path):
    import agent.models_dev as mdev
    from hermes_cli import model_switch

    attempts: list = []

    def _no_network(*args, **kwargs):
        attempts.append(args[:1])
        raise OSError("network disabled in test")

    # Cold: no in-memory cache, no disk cache, no failure backoff.
    monkeypatch.setattr(mdev, "_models_dev_cache", {}, raising=False)
    monkeypatch.setattr(mdev, "_models_dev_cache_time", 0, raising=False)
    monkeypatch.setattr(mdev, "_models_dev_retry_after", 0, raising=False)
    monkeypatch.setattr(mdev, "_get_cache_path", lambda: tmp_path / "models_dev_cache.json", raising=False)
    fetched: list = []
    real_fetch = mdev.fetch_models_dev

    def _spy_fetch(*a, **k):
        fetched.append(k.get("allow_network", True))
        return real_fetch(*a, **k)

    monkeypatch.setattr(mdev, "fetch_models_dev", _spy_fetch)
    monkeypatch.setattr(socket, "create_connection", _no_network)
    monkeypatch.setattr(socket.socket, "connect", lambda self, addr: _no_network(addr))

    user_providers = {
        "local-test": {
            "name": "local-test",
            "base_url": "http://127.0.0.1:9/v1",
            "api_key": "sk-test",
            "models": {"m-1": {}},
        }
    }
    model_switch.switch_model(
        raw_input="m-1", current_provider="local-test", current_model="m-1",
        explicit_provider="local-test", user_providers=user_providers,
        custom_providers=[], probe_catalog=False,
    )
    assert True not in fetched, f"probe_catalog=False fetched models.dev with network allowed: {fetched}"
    assert attempts == [], f"probe_catalog=False opened a socket on a cold cache: {attempts}"
