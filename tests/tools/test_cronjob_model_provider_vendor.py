"""cronjob create/update refuses a model pinned to a provider of a different vendor.

Such a job (e.g. a ``gpt-*`` model on ``anthropic``) is accepted, persisted, and then fails
every fire with HTTP 400. The check uses the provider catalog and fails open on anything it
cannot place (unknown names, aggregators, custom providers, explicit base_url).
"""
import json

import pytest

from tools.cronjob_tools import _model_provider_vendor_error, cronjob


@pytest.fixture(autouse=True)
def _cron_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")


def _create(**kw):
    return json.loads(cronjob(action="create", prompt="Check server status", schedule="every 1h",
                              name="Vendor Check", deliver="local", repeat=3, **kw))


def _error(result):
    return result.get("error") or result.get("message") or ""


class TestPredicate:
    def test_cross_vendor_pair_is_refused(self):
        err = _model_provider_vendor_error("gpt-5.6-sol", "anthropic")
        assert err and "openai" in err and "anthropic" in err and "retrying will not help" in err

    def test_matching_pairs_pass(self):
        assert _model_provider_vendor_error("gpt-5.6-sol", "openai-codex") is None
        assert _model_provider_vendor_error("claude-opus-5", "anthropic") is None

    @pytest.mark.parametrize("model,provider,base_url", [
        ("mystery-7b", "anthropic", None),          # unknown model vendor
        ("gpt-5.6-sol", "openrouter", None),        # aggregator serves many vendors
        ("gpt-5.6-sol", "my-private-proxy", None),  # provider not in the catalog
        ("gpt-5.6-sol", "custom", None),
        ("gpt-5.6-sol", "anthropic", "https://llm.example.com/v1"),  # explicit endpoint
        (None, "anthropic", None),
        ("gpt-5.6-sol", None, None),
    ])
    def test_fails_open_on_what_it_cannot_place(self, model, provider, base_url):
        assert _model_provider_vendor_error(model, provider, base_url) is None

    def test_dict_model_shape(self):
        assert _model_provider_vendor_error({"model": "gpt-5.6-sol", "provider": "anthropic"}, None)
        assert _model_provider_vendor_error({"model": "gpt-5.6-sol"}, "anthropic")
        assert _model_provider_vendor_error({"model": "gpt-5.6-sol", "provider": "openai-codex"},
                                            "openai-codex") is None


class TestCreate:
    def test_cross_vendor_create_is_refused_and_not_persisted(self):
        result = _create(model="gpt-5.6-sol", provider="anthropic")
        assert result["success"] is False
        assert "gpt-5.6-sol" in _error(result) and "anthropic" in _error(result)
        assert json.loads(cronjob(action="list"))["count"] == 0

    def test_matching_create_is_allowed(self):
        assert _create(model="gpt-5.6-sol", provider="openai-codex")["success"] is True


class TestUpdate:
    def _pinned(self):
        created = _create(model="gpt-5.6-sol", provider="openai-codex")
        assert created["success"] is True
        return created["job"]["job_id"]

    def test_changing_only_the_provider_is_checked_against_the_stored_model(self):
        result = json.loads(cronjob(action="update", job_id=self._pinned(), provider="anthropic"))
        assert result["success"] is False

    def test_changing_only_the_model_is_checked_against_the_stored_provider(self):
        result = json.loads(cronjob(action="update", job_id=self._pinned(), model="claude-opus-5"))
        assert result["success"] is False

    def test_consistent_pair_update_is_allowed(self):
        result = json.loads(cronjob(action="update", job_id=self._pinned(),
                                    model="claude-opus-5", provider="anthropic"))
        assert result["success"] is True

    def test_unrelated_update_is_allowed(self):
        assert json.loads(cronjob(action="update", job_id=self._pinned(), name="Renamed"))["success"] is True
