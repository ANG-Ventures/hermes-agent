"""Regression: a cron job whose ``model`` is the structured dict form must resolve.

Bug (observed live 2026-08-06, job ``resolution-upgrade-monitor``):
a job stored ``model={"model": "gpt-5.6-sol", "provider": "openai-codex"}`` —
the structured form written by older schemas / hand edits rather than the plain
string the current write path normalizes to.

That dict is TRUTHY, which broke model resolution in a self-contradicting way:

  * ``model = job.get("model") or os.getenv(...) or ""`` happily accepted the
    dict, so ``model`` became a dict;
  * the config fallback was gated on ``if not job.get("model")`` — truthy, so
    ``config.yaml`` was never consulted;
  * the fail-fast guard then checked ``isinstance(model, str)``, which a dict
    fails, and raised::

        Cron job 'resolution-upgrade-monitor' has no model configured
        (job.model={'model': 'gpt-5.6-sol', 'provider': 'openai-codex'},
         HERMES_MODEL='', config.yaml model.default missing or empty)

    i.e. it reported "no model configured" while quoting a model that was
    plainly present, and claimed ``model.default`` was missing when it was set.

A sibling site (the ``model_snapshot`` drift check) called ``.strip()`` on the
same field and raised ``AttributeError: 'dict' object has no attribute 'strip'``.

Fix: ``_coerce_job_model`` flattens the field to the plain string form before
any truthiness test, isinstance guard, or ``.strip()`` call.
"""
import os
import re

import pytest

SCHEDULER = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "cron",
    "scheduler.py",
)


def _load_coerce():
    """Exec just the helper, so this test doesn't drag in the scheduler's imports."""
    src = open(SCHEDULER, encoding="utf-8").read()
    m = re.search(r"def _coerce_job_model.*?\n    return None\n", src, re.S)
    assert m, "_coerce_job_model not found in cron/scheduler.py"
    ns = {}
    exec(m.group(0), ns)  # noqa: S102
    return ns["_coerce_job_model"]


coerce_job_model = _load_coerce()


class TestCoerceJobModel:
    def test_dict_form_yields_the_inner_model_string(self):
        """THE BUG: the structured form must flatten, not be passed through."""
        got = coerce_job_model({"model": "gpt-5.6-sol", "provider": "openai-codex"})
        assert got == "gpt-5.6-sol"
        assert isinstance(got, str)

    def test_plain_string_is_preserved(self):
        assert coerce_job_model("claude-opus-5") == "claude-opus-5"

    def test_whitespace_is_stripped(self):
        assert coerce_job_model("  gpt-5.6-sol  ") == "gpt-5.6-sol"

    @pytest.mark.parametrize(
        "value",
        [None, "", "   ", {}, {"provider": "openai-codex"}, 123, [], object()],
    )
    def test_unusable_values_yield_none(self, value):
        """None is the contract for 'no model here' so env/config fallback runs."""
        assert coerce_job_model(value) is None

    def test_dict_alias_keys(self):
        assert coerce_job_model({"name": "gpt-5.6-terra"}) == "gpt-5.6-terra"
        assert coerce_job_model({"default": "claude-opus-5"}) == "claude-opus-5"


class TestResolutionPathRegression:
    """Replay the exact scheduler logic around the two crash sites."""

    DICT_JOB = {
        "model": {"model": "gpt-5.6-sol", "provider": "openai-codex"},
        "name": "resolution-upgrade-monitor",
        "model_snapshot": "claude-opus-5",
    }

    def test_fail_fast_guard_no_longer_raises_for_a_dict_model(self, monkeypatch):
        monkeypatch.setenv("HERMES_MODEL", "")
        job_model = coerce_job_model(self.DICT_JOB.get("model"))
        model = job_model or os.getenv("HERMES_MODEL") or ""
        # the guard that used to fire
        assert isinstance(model, str) and model.strip(), (
            "dict-form model must resolve to a non-empty string"
        )
        assert model == "gpt-5.6-sol"

    def test_config_fallback_is_skipped_because_the_job_really_is_pinned(self):
        """The gate must key on the COERCED value, not raw truthiness."""
        job_model = coerce_job_model(self.DICT_JOB.get("model"))
        assert job_model, "a pinned job must not fall through to config.yaml"

    def test_unpinned_job_still_falls_back(self):
        """Guard against over-correcting: no model => fallback must still run."""
        assert coerce_job_model(None) is None
        assert coerce_job_model({}) is None

    def test_model_snapshot_drift_gate_does_not_attributeerror(self):
        """Sibling site: ``(job.get('model') or '').strip()`` crashed on a dict."""
        snapshot = (self.DICT_JOB.get("model_snapshot") or "").strip().lower()
        # must evaluate without raising AttributeError
        gate = bool(snapshot and not (coerce_job_model(self.DICT_JOB.get("model")) or ""))
        assert gate is False, "a pinned job is not 'unpinned drift'"

    def test_old_code_would_have_failed(self):
        """Pin the defect so a regression is unambiguous."""
        raw = self.DICT_JOB["model"]
        assert raw, "dict is truthy — this is why the old `or` chain accepted it"
        assert not isinstance(raw, str), "…and why the isinstance guard then rejected it"
        with pytest.raises(AttributeError):
            (raw or "").strip()
