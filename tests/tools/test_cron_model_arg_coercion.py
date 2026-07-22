"""cronjob ``model`` argument coercion — behavior contracts.

The ``cronjob`` tool schema documents ``model`` as an OBJECT
(``{"model": ..., "provider": ...}``), but callers routinely pass a flat
model-name STRING plus a sibling ``provider`` arg (the shape every other
model-bearing tool uses). Before this fix that flat string silently failed
``_resolve_model_override``'s ``isinstance(dict)`` guard → ``(None, None)`` →
the job fell through to ``cron.default_model`` (often ``auto``) and pinned to
the CREATING agent's model instead of the requested one — an explicit input
SILENTLY dropped (observed live 2026-07-21: three create calls each meant to
pin gpt-5.6-sol, each landed on the session's fable/haiku model).

The fix: ``_coerce_model_override_arg`` accepts BOTH shapes; a truthy but
uninterpretable value is dropped WITH a surfaced warning rather than silently.

These are invariant tests: they assert the arg-shape → resolved-pin RELATION
and the warn-on-silent-drop contract, not a frozen model list.
"""
import json

import pytest

import tools.cronjob_tools as ct


def _clear_agent_model():
    ct.set_current_agent_model(None, None)


# --- the pure coercion helper -------------------------------------------

def test_coerce_flat_string_folds_sibling_provider():
    obj, warn = ct._coerce_model_override_arg("gpt-5.6-sol", "openai-codex")
    assert obj == {"model": "gpt-5.6-sol", "provider": "openai-codex"}
    assert warn is None


def test_coerce_flat_string_without_provider():
    obj, warn = ct._coerce_model_override_arg("gpt-5.6-sol", None)
    assert obj == {"model": "gpt-5.6-sol"}
    assert warn is None


def test_coerce_canonical_object_passes_through():
    src = {"model": "claude-opus-4-8", "provider": "claude-apr"}
    obj, warn = ct._coerce_model_override_arg(src, None)
    assert obj == src
    assert warn is None


def test_coerce_object_wins_over_sibling_provider():
    # An explicit object provider is authoritative; the flat sibling provider
    # arg does not override it.
    obj, warn = ct._coerce_model_override_arg(
        {"model": "claude-opus-4-8", "provider": "claude-apr"}, "openai-codex"
    )
    assert obj == {"model": "claude-opus-4-8", "provider": "claude-apr"}
    assert warn is None


def test_coerce_none_is_silent():
    obj, warn = ct._coerce_model_override_arg(None, None)
    assert obj is None
    assert warn is None


def test_coerce_empty_string_is_silent():
    obj, warn = ct._coerce_model_override_arg("   ", None)
    assert obj is None
    assert warn is None


def test_coerce_uninterpretable_value_warns_not_silent():
    # A truthy but non-str/non-dict spec must NOT be silently dropped.
    for bad in (["gpt-5.6-sol"], 42, {"model"}):
        obj, warn = ct._coerce_model_override_arg(bad, None)
        assert obj is None
        assert warn is not None
        assert "model spec ignored" in warn


def test_coerce_preserves_auto_sentinel():
    obj, warn = ct._coerce_model_override_arg("auto", None)
    assert obj == {"model": "auto"}
    assert warn is None


# --- handler → create integration (the real bug reproduction) -----------

class TestHandlerFlatStringPins:
    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, monkeypatch):
        monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
        monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
        monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
        # cron.default_model=auto is the live config that USED to swallow the
        # flat-string spec — reproduce it so the test proves the fix under the
        # exact condition that bit in production.
        monkeypatch.setattr(
            "hermes_cli.config.load_config",
            lambda: {"cron": {"default_model": "auto"}},
        )
        # Publish a DIFFERENT agent model, so a silent-drop would pin THIS,
        # not the requested one — makes the bug visible if it regresses.
        ct.set_current_agent_model("claude-apr", "claude-fable-5")
        yield
        ct.set_current_agent_model(None, None)

    def test_flat_string_model_pins_requested_not_agent(self):
        # The exact failing 2026-07-21 call shape: flat model + flat provider.
        out = json.loads(ct._cronjob_tool_handler({
            "action": "create",
            "prompt": "Check",
            "schedule": "every 1h",
            "name": "flat-string-pin",
            "model": "gpt-5.6-sol",
            "provider": "openai-codex",
        }))
        assert out["success"] is True
        # Must pin the REQUESTED model, NOT the creating agent's fable model.
        assert out["job"]["model"] == "gpt-5.6-sol"
        assert out["job"]["provider"] == "openai-codex"

    def test_object_shape_still_works(self):
        out = json.loads(ct._cronjob_tool_handler({
            "action": "create",
            "prompt": "Check",
            "schedule": "every 1h",
            "name": "object-pin",
            "model": {"model": "gpt-5.6-sol", "provider": "openai-codex"},
        }))
        assert out["success"] is True
        assert out["job"]["model"] == "gpt-5.6-sol"
        assert out["job"]["provider"] == "openai-codex"

    def test_auto_still_pins_agent(self):
        # Back-compat: model="auto" still resolves to the creating agent.
        out = json.loads(ct._cronjob_tool_handler({
            "action": "create",
            "prompt": "Check",
            "schedule": "every 1h",
            "name": "auto-pin",
            "model": "auto",
        }))
        assert out["success"] is True
        assert out["job"]["model"] == "claude-fable-5"
        assert out["job"]["provider"] == "claude-apr"

    def test_uninterpretable_model_warns_in_result(self):
        out = json.loads(ct._cronjob_tool_handler({
            "action": "create",
            "prompt": "Check",
            "schedule": "every 1h",
            "name": "bad-spec",
            "model": ["gpt-5.6-sol"],  # wrong type
        }))
        assert out["success"] is True  # job still created (auto-pinned)
        assert any("model spec ignored" in w for w in out.get("warnings", []))
