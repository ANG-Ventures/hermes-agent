"""`hermes chat -m <alias>` / `-m <provider>/<model>` must resolve like `/model`.

Regression coverage for the silent-fallback class measured 2026-09-18:
`hermes chat -m grok` (a config `model.aliases` entry) sent the literal word
``grok`` to the current provider, which 400'd, and the fallback chain served
``claude-apx-1/claude-opus-5`` instead — a different provider AND model — with
only a one-line banner. Same for ``-m xai-oauth/grok-4.6``.
"""
import pytest

from hermes_cli import model_switch as ms


@pytest.fixture(autouse=True)
def _reset_direct_aliases():
    saved = dict(ms.DIRECT_ALIASES)
    saved_degraded = ms._DIRECT_ALIASES_DEGRADED
    ms.DIRECT_ALIASES.clear()
    ms._DIRECT_ALIASES_DEGRADED = False
    yield
    ms.DIRECT_ALIASES.clear()
    ms.DIRECT_ALIASES.update(saved)
    ms._DIRECT_ALIASES_DEGRADED = saved_degraded


def _fake_aliases(monkeypatch, aliases_map):
    def _loader():
        merged = dict(ms._BUILTIN_DIRECT_ALIASES)
        for k, v in aliases_map.items():
            prov, model = v.split("/", 1)
            merged[k] = ms.DirectAlias(model=model, provider=prov, base_url="")
        return merged, True
    monkeypatch.setattr(ms, "_load_direct_aliases", _loader)


def test_config_alias_resolves_to_its_provider_and_model(monkeypatch):
    _fake_aliases(monkeypatch, {"grok": "xai-oauth/grok-4.6"})
    provider, model = ms.resolve_startup_model_arg("grok", "claude-apr")
    assert (provider, model) == ("xai-oauth", "grok-4.6")


def test_alias_lookup_is_case_and_whitespace_insensitive(monkeypatch):
    _fake_aliases(monkeypatch, {"grok": "xai-oauth/grok-4.6"})
    assert ms.resolve_startup_model_arg("  GROK ", "claude-apr") == ("xai-oauth", "grok-4.6")


def test_inline_provider_slash_form_is_split(monkeypatch):
    _fake_aliases(monkeypatch, {})
    # xai-oauth is a real registered provider id; claude-apr is not an aggregator
    monkeypatch.setattr(ms, "is_aggregator", lambda p: False)
    provider, model = ms.resolve_startup_model_arg("xai-oauth/grok-4.6", "claude-apr")
    assert (provider, model) == ("xai-oauth", "grok-4.6")


def test_inline_provider_colon_form_is_split(monkeypatch):
    _fake_aliases(monkeypatch, {})
    provider, model = ms.resolve_startup_model_arg("xai-oauth:grok-4.6", "claude-apr")
    assert (provider, model) == ("xai-oauth", "grok-4.6")


@pytest.mark.parametrize("raw", [
    "anthropic/claude-opus-4.6",
    "openai/gpt-5.4",
    "meta-llama/llama-4-scout",
    "deepseek/deepseek-v4-flash",
])
def test_vendor_namespace_is_not_a_provider_switch(monkeypatch, raw):
    """`vendor/model` is a model-id prefix the TARGET provider strips, not an
    inline provider switch. Hijacking it here routed `-m anthropic/claude-opus-4.6`
    away from the configured provider instead of stripping to the bare id, which
    regressed the foreign-provider-prefix incident guard
    (tests/hermes_cli/test_codex_foreign_provider_prefix.py)."""
    _fake_aliases(monkeypatch, {})
    assert ms.resolve_startup_model_arg(raw, "openai-codex") == (None, raw)


def test_plain_model_id_passes_through_unchanged(monkeypatch):
    _fake_aliases(monkeypatch, {"grok": "xai-oauth/grok-4.6"})
    assert ms.resolve_startup_model_arg("claude-opus-5", "claude-apr") == (None, "claude-opus-5")


def test_moa_prefix_is_left_for_the_moa_normalizer(monkeypatch):
    _fake_aliases(monkeypatch, {"grok": "xai-oauth/grok-4.6"})
    assert ms.resolve_startup_model_arg("moa:default", "claude-apr") == (None, "moa:default")


def test_none_and_empty_are_untouched(monkeypatch):
    _fake_aliases(monkeypatch, {})
    assert ms.resolve_startup_model_arg(None, "claude-apr") == (None, None)
    assert ms.resolve_startup_model_arg("", "claude-apr") == (None, "")


def test_aggregator_vendor_slug_is_not_split(monkeypatch):
    """On an aggregator, `vendor/model` is a model id, not a provider switch."""
    _fake_aliases(monkeypatch, {})
    provider, model = ms.resolve_startup_model_arg("x-ai/grok-4.6", "openrouter")
    assert (provider, model) == (None, "x-ai/grok-4.6")


def test_cli_init_wires_resolver_into_requested_provider():
    """Lock the WIRING, not just the helper: HermesCLI.__init__ must call
    resolve_startup_model_arg on the explicit -m value and feed the returned
    provider into requested_provider ahead of the config default."""
    import ast
    import inspect
    import textwrap

    import cli as cli_mod

    src = textwrap.dedent(inspect.getsource(cli_mod.HermesCLI.__init__))
    assert "resolve_startup_model_arg(" in src, "startup -m alias resolution not called in HermesCLI.__init__"
    tree = ast.parse(src)
    order = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Attribute) and t.attr == "requested_provider" for t in node.targets
        ) and isinstance(node.value, ast.BoolOp):
            order = [getattr(v, "id", None) or getattr(v, "attr", None) for v in node.value.values]
            break
    assert order is not None, "requested_provider precedence chain not found"
    assert "_inline_provider_override" in order, order
    # precedence: moa > --provider > inline/alias > nested-config-default
    assert (order.index("_moa_provider_override") < order.index("provider")
            < order.index("_inline_provider_override") < order.index("_nested_provider")), order
