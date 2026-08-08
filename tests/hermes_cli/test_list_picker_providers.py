"""Tests for ``list_picker_providers`` — the /model picker filter.

``list_picker_providers`` wraps ``list_authenticated_providers`` and
post-processes the result for interactive pickers (Telegram, Discord):

- OpenRouter's ``models`` are replaced with the live-filtered output of
  ``fetch_openrouter_models``, so IDs the live catalog no longer carries
  drop out.
- Provider rows with an empty ``models`` list are dropped, except custom
  endpoints (``is_user_defined=True`` with an ``api_url``) where the user
  may supply their own model set through config.

These tests exercise the filter in isolation by mocking
``list_authenticated_providers`` and ``fetch_openrouter_models`` so no
network or auth state is required.
"""

import pytest
from hermes_cli import model_switch


@pytest.fixture(autouse=True)
def _disable_live_custom_provider_model_probe(monkeypatch):
    """Keep custom-provider picker fixtures independent of local model servers."""
    monkeypatch.setattr("hermes_cli.models.fetch_api_models", lambda *_a, **_kw: None)


def _make_provider(slug, name=None, models=None, *, is_current=False,
                   is_user_defined=False, source="built-in", api_url=None):
    """Build a dict shaped like ``list_authenticated_providers`` output."""
    entry = {
        "slug": slug,
        "name": name or slug.title(),
        "is_current": is_current,
        "is_user_defined": is_user_defined,
        "models": list(models or []),
        "total_models": len(models or []),
        "source": source,
    }
    if api_url is not None:
        entry["api_url"] = api_url
    return entry


def test_passthrough_kwargs_to_base(monkeypatch):
    """All kwargs must be forwarded to ``list_authenticated_providers`` unchanged.

    The gateway /model picker passes ``current_base_url`` and ``current_model``
    so custom endpoint grouping can mark the current row. Dropping those kwargs
    regressed Telegram/Discord into the text-list fallback.
    """
    captured = {}

    def _capture(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(model_switch, "list_authenticated_providers", _capture)
    monkeypatch.setattr("hermes_cli.models.fetch_openrouter_models",
                        lambda *a, **kw: [])

    model_switch.list_picker_providers(
        current_provider="openrouter",
        current_base_url="http://x",
        current_model="openai/gpt-5.4",
        user_providers={"foo": {"api": "http://x"}},
        custom_providers=[{"name": "bar", "base_url": "http://y"}],
        max_models=12,
    )

    assert captured["current_provider"] == "openrouter"
    assert captured["current_base_url"] == "http://x"
    assert captured["current_model"] == "openai/gpt-5.4"
    assert captured["user_providers"] == {"foo": {"api": "http://x"}}
    assert captured["custom_providers"] == [{"name": "bar", "base_url": "http://y"}]
    assert captured["max_models"] == 12


def test_current_model_not_duplicated_when_catalog_entry_is_namespaced(monkeypatch):
    """Regression: the current model must not appear twice in the picker.

    ``current_model`` is stored bare in config (e.g. ``claude-opus-4-8``) while
    a provider's curated catalog lists it namespaced (``claude-app/claude-opus-4-8``).
    The post-pass that injects the current model at the top of the current row
    used a plain ``current_model not in _models`` check, which never matched the
    namespaced entry — so the model was injected a SECOND time and the platform
    pickers (Discord/Telegram strip the prefix for display) showed it twice.

    Here the current custom row's catalog is the bare + namespaced pair; a bare
    ``current_model`` must be recognised as already present via the namespaced
    entry and NOT re-injected.
    """
    monkeypatch.setattr("agent.models_dev.fetch_models_dev", lambda: {})
    monkeypatch.setattr("agent.models_dev.PROVIDER_TO_MODELS_DEV", {})
    monkeypatch.setattr("hermes_cli.providers.HERMES_OVERLAYS", {})
    monkeypatch.setattr("hermes_cli.models.fetch_openrouter_models",
                        lambda *a, **kw: [])

    # The custom-endpoint branch seeds the row's models from ``current_model``.
    # Give the row a namespaced catalog entry via the config ``model`` list so
    # the injected bare id would collide on display but not on a naive ``in``.
    result = model_switch.list_authenticated_providers(
        current_provider="custom:relay",
        current_base_url="http://localhost:9099/v1",
        current_model="claude-opus-4-8",
        custom_providers=[
            {
                "name": "Relay",
                "base_url": "http://localhost:9099/v1",
                "api_key": "x",
                "models": ["relay/claude-opus-4-8", "relay/claude-sonnet-5"],
            },
        ],
    )

    current_rows = [p for p in result if p.get("is_current")]
    assert current_rows, "expected a current row"
    row = current_rows[0]
    display = [m.split("/")[-1] for m in row["models"]]
    # The bare current model resolves to the namespaced entry — no duplicate.
    assert display.count("claude-opus-4-8") == 1, row["models"]
    assert row["total_models"] == len(row["models"])


def test_current_model_injected_when_genuinely_absent(monkeypatch):
    """Guard the other side: an uncurated current model IS still injected.

    The namespace-aware check must not over-match — a model the catalog does
    not carry (in any namespaced form) must still be prepended so it stays
    selectable in the picker.
    """
    monkeypatch.setattr("agent.models_dev.fetch_models_dev", lambda: {})
    monkeypatch.setattr("agent.models_dev.PROVIDER_TO_MODELS_DEV", {})
    monkeypatch.setattr("hermes_cli.providers.HERMES_OVERLAYS", {})
    monkeypatch.setattr("hermes_cli.models.fetch_openrouter_models",
                        lambda *a, **kw: [])

    result = model_switch.list_authenticated_providers(
        current_provider="custom:relay",
        current_base_url="http://localhost:9099/v1",
        current_model="some-uncurated-model",
        custom_providers=[
            {
                "name": "Relay",
                "base_url": "http://localhost:9099/v1",
                "api_key": "x",
                "models": ["relay/claude-opus-4-8"],
            },
        ],
    )

    current_rows = [p for p in result if p.get("is_current")]
    assert current_rows, "expected a current row"
    row = current_rows[0]
    assert "some-uncurated-model" in row["models"], row["models"]


def test_numbered_failover_lanes_hidden_from_picker(monkeypatch):
    """claude-apx-N / claude-bpx-N failover lanes are hidden from the picker.

    They are internal auto-failover targets, not hand-selectable providers, and
    20+ of them crowd real providers out of the dropdown's 25-option cap. N
    INCLUDES 0 (claude-bpx-0 / claude-apx-0 are lanes too). They must NOT appear
    in the picker; the relay pools (claude-apr / claude-bpr) and everything else
    must survive.
    """
    base = [
        _make_provider("anthropic", models=["claude-opus-4-8"]),
        _make_provider("claude-apr", models=["claude-opus-4-8"]),
        _make_provider("claude-bpr", models=["claude-opus-4-8"]),
        _make_provider("claude-apx-0", models=["claude-opus-4-8"]),
        _make_provider("claude-apx-1", models=["claude-opus-4-8"]),
        _make_provider("claude-apx-10", models=["claude-opus-4-8"]),
        _make_provider("claude-bpx-0", models=["claude-opus-4-8"]),
        _make_provider("claude-bpx-5", models=["claude-opus-4-8"]),
        _make_provider("yunwu", models=["claude-opus-4-8"]),
    ]

    monkeypatch.setattr(model_switch, "list_authenticated_providers",
                        lambda **kw: list(base))
    monkeypatch.setattr("hermes_cli.models.fetch_openrouter_models",
                        lambda *a, **kw: pytest.fail("should not be called"))

    result = [p["slug"] for p in model_switch.list_picker_providers(max_models=50)]

    # Every numbered lane — INCLUDING -0 — is gone.
    for lane in ("claude-apx-0", "claude-apx-1", "claude-apx-10",
                 "claude-bpx-0", "claude-bpx-5"):
        assert lane not in result, f"{lane} should be hidden"
    # Relay pools + real providers survive.
    for keep in ("anthropic", "claude-apr", "claude-bpr", "yunwu"):
        assert keep in result, f"{keep} should remain visible"


def test_current_failover_lane_stays_visible(monkeypatch):
    """A numbered lane is kept ONLY when it's the currently-active provider.

    So a user who is actually running on claude-bpx-5 can still see it in
    the picker (to switch away), while the other lanes stay hidden.
    """
    base = [
        _make_provider("claude-bpx-1", models=["claude-opus-4-8"]),
        _make_provider("claude-bpx-5", models=["claude-opus-4-8"],
                       is_current=True),
        _make_provider("yunwu", models=["claude-opus-4-8"]),
    ]

    monkeypatch.setattr(model_switch, "list_authenticated_providers",
                        lambda **kw: list(base))
    monkeypatch.setattr("hermes_cli.models.fetch_openrouter_models",
                        lambda *a, **kw: pytest.fail("should not be called"))

    result = [p["slug"] for p in model_switch.list_picker_providers(
        current_provider="claude-bpx-5", max_models=50)]

    assert "claude-bpx-5" in result   # current lane visible
    assert "claude-bpx-1" not in result  # other lanes still hidden
    assert "yunwu" in result


def test_non_failover_claude_providers_never_hidden(monkeypatch):
    """The hide-rule must be surgical: only claude-{apx,bpx}-N (N any int) match.

    Relay pools and unrelated slugs that merely contain 'apx'/'bpx' text must
    not be swept up by the failover-lane regex.
    """
    base = [
        _make_provider("claude-apr"),        # relay pool, not a lane
        _make_provider("claude-bpr"),        # relay pool, not a lane
        _make_provider("claude-app"),        # legacy base, not a lane
        _make_provider("claude-apxtra"),     # 'apx' substring but not -N
    ]
    for p in base:
        p["models"] = ["m"]

    monkeypatch.setattr(model_switch, "list_authenticated_providers",
                        lambda **kw: list(base))
    monkeypatch.setattr("hermes_cli.models.fetch_openrouter_models",
                        lambda *a, **kw: pytest.fail("should not be called"))

    result = [p["slug"] for p in model_switch.list_picker_providers(max_models=50)]

    assert result == [
        "claude-apr", "claude-bpr", "claude-app", "claude-apxtra",
    ]


# --- model.picker preferences (hide + order) ---------------------------------

def _picker_cfg(monkeypatch, hide=None, order=None):
    """Mock hermes_cli.config.load_config to return a model.picker block."""
    picker = {}
    if hide is not None:
        picker["hide"] = hide
    if order is not None:
        picker["order"] = order
    monkeypatch.setattr("hermes_cli.config.load_config",
                        lambda: {"model": {"picker": picker}})


def _rows(*slugs):
    return [{"slug": s, "name": s, "models": ["m"]} for s in slugs]


def test_picker_hide_drops_listed_providers(monkeypatch):
    """model.picker.hide drops the named providers from the dropdown."""
    _picker_cfg(monkeypatch, hide=["openai-api", "anthropic"])
    out = model_switch._apply_picker_preferences(
        _rows("openrouter", "anthropic", "openai-api", "yunwu"))
    assert [r["slug"] for r in out] == ["openrouter", "yunwu"]


def test_picker_hide_never_hides_current_provider(monkeypatch):
    """The currently-active provider is never hidden, even if listed."""
    _picker_cfg(monkeypatch, hide=["anthropic"])
    out = model_switch._apply_picker_preferences(
        _rows("openrouter", "anthropic", "yunwu"),
        current_provider="anthropic")
    assert "anthropic" in [r["slug"] for r in out]


def test_picker_order_front_anchors_listed_slugs(monkeypatch):
    """model.picker.order moves listed slugs to the front in that order; the
    rest keep their original relative order after them."""
    _picker_cfg(monkeypatch,
                order=["claude-apr", "claude-bpr", "yunwu"])
    out = model_switch._apply_picker_preferences(
        _rows("openrouter", "yunwu", "claude-bpr", "anthropic", "claude-apr"))
    assert [r["slug"] for r in out] == [
        "claude-apr", "claude-bpr", "yunwu",   # front, in order
        "openrouter", "anthropic",             # rest, original order
    ]


def test_picker_hide_and_order_together(monkeypatch):
    """The full requested shape: hide two, order the rest, unlisted trail."""
    _picker_cfg(
        monkeypatch,
        hide=["openai-api", "anthropic"],
        order=["claude-apr", "claude-bpr", "openai-codex",
               "gemini-bridge", "openrouter", "yunwu"],
    )
    out = model_switch._apply_picker_preferences(_rows(
        "openrouter", "gemini-bridge", "anthropic", "openai-api",
        "openai-codex", "claude-apr", "claude-app", "claude-bpr", "yunwu",
    ))
    assert [r["slug"] for r in out] == [
        "claude-apr", "claude-bpr", "openai-codex",
        "gemini-bridge", "openrouter", "yunwu",
        "claude-app",  # not listed in order/hide → trails, kept
    ]


def test_picker_prefs_noop_when_config_absent(monkeypatch):
    """No model.picker config → rows returned unchanged."""
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"model": {}})
    rows = _rows("openrouter", "anthropic", "yunwu")
    out = model_switch._apply_picker_preferences(rows)
    assert [r["slug"] for r in out] == ["openrouter", "anthropic", "yunwu"]


def test_picker_prefs_unknown_slugs_ignored(monkeypatch):
    """Unknown slugs in hide/order are ignored (no empty rows, no crash)."""
    _picker_cfg(monkeypatch, hide=["does-not-exist"],
                order=["also-missing", "yunwu"])
    out = model_switch._apply_picker_preferences(
        _rows("openrouter", "yunwu"))
    assert [r["slug"] for r in out] == ["yunwu", "openrouter"]


def test_picker_order_with_blank_entries_still_front_anchors(monkeypatch):
    """Blank/empty entries in order must not corrupt the fallback rank.

    Regression for the len(rank)+1 collision (Greptile #240 P2): with
    order=["a","","","c"], rank={a:0,c:3}; a naive len(rank)+1 == 3 fallback
    would tie with c's rank and a stable sort could leave an unlisted row
    ahead of c. Using len(order)+1 keeps every listed slug strictly ahead of
    every unlisted one.
    """
    _picker_cfg(monkeypatch, order=["a", "", "", "c"])
    out = model_switch._apply_picker_preferences(
        _rows("unlisted", "c", "a"))
    slugs = [r["slug"] for r in out]
    assert slugs == ["a", "c", "unlisted"], slugs
    # both listed slugs strictly precede the unlisted row
    assert slugs.index("c") < slugs.index("unlisted")


def test_picker_prefs_whitespace_slug_matches(monkeypatch):
    """Row slugs with surrounding whitespace still hide/order correctly.

    Regression for the strip asymmetry (Greptile #240 P2): keys were built with
    .strip().lower() but lookups only .lower(); a padded slug slipped through.
    """
    _picker_cfg(monkeypatch, hide=["anthropic"], order=["yunwu"])
    rows = [
        {"slug": " anthropic ", "name": "A", "models": ["m"]},
        {"slug": "  yunwu", "name": "Y", "models": ["m"]},
        {"slug": "openrouter", "name": "O", "models": ["m"]},
    ]
    out = [r["slug"] for r in model_switch._apply_picker_preferences(rows)]
    assert " anthropic " not in out            # hidden despite whitespace
    assert out[0] == "  yunwu"                  # ordered first despite whitespace


# ---------------------------------------------------------------------------
# list_authenticated_providers: alias/canonical de-dup for Kimi (#49439)
# ---------------------------------------------------------------------------
#
# A single Kimi credential used to surface TWO picker rows: the alias slug
# "kimi" (emitted by the PROVIDER_TO_MODELS_DEV pass) plus its canonical
# "kimi-coding" (re-emitted by the CANONICAL_PROVIDERS cross-check pass),
# both backed by the same kimi-for-coding models.dev provider. The picker
# must list each authenticated credential exactly once, under the CANONICAL
# slug ("kimi-coding") — matching list_authenticated_providers' other alias
# rows and the overlay slug-resolution contract (see
# test_overlay_slug_resolution.py).


def _stub_kimi_discovery(monkeypatch, *, canonical):
    """Isolate list_authenticated_providers to the Kimi alias family.

    Restricts the models.dev map / catalog / overlays / canonical list to
    just the Kimi entries and stubs the model-id fetch so discovery stays
    offline and deterministic. ``canonical`` is the CANONICAL_PROVIDERS list
    the 2b cross-check pass should iterate.
    """
    import agent.models_dev as md
    import hermes_cli.models as hm

    kimi_map = {
        "kimi": "kimi-for-coding",
        "kimi-coding": "kimi-for-coding",
        "moonshot": "kimi-for-coding",
        "kimi-coding-cn": "kimi-for-coding",
    }
    monkeypatch.setattr(md, "PROVIDER_TO_MODELS_DEV", kimi_map)
    monkeypatch.setattr(
        md, "fetch_models_dev",
        lambda *a, **k: {
            "kimi-for-coding": {"name": "Kimi For Coding", "env": ["KIMI_API_KEY"]},
        },
    )

    class _PInfo:
        name = "Kimi For Coding"

    monkeypatch.setattr(md, "get_provider_info", lambda _pid: _PInfo())
    monkeypatch.setattr("hermes_cli.providers.HERMES_OVERLAYS", {})
    monkeypatch.setattr(hm, "CANONICAL_PROVIDERS", canonical)
    monkeypatch.setattr(hm, "cached_provider_model_ids",
                        lambda *a, **k: ["kimi-k2.6", "kimi-k2.5"])
    monkeypatch.setattr(hm, "clear_provider_models_cache", lambda *a, **k: None)


def test_single_kimi_credential_yields_one_canonical_row(monkeypatch):
    """One Kimi key yields a single row under the canonical 'kimi-coding' slug."""
    import hermes_cli.models as hm

    _stub_kimi_discovery(
        monkeypatch,
        canonical=[hm.ProviderEntry("kimi-coding", "Kimi / Kimi Coding Plan", "desc")],
    )
    monkeypatch.setenv("KIMI_API_KEY", "sk-test-kimi")

    rows = model_switch.list_authenticated_providers(max_models=10)
    slugs = [r["slug"] for r in rows]

    # Exactly one Kimi / kimi-for-coding-backed row, under the canonical slug —
    # not both the alias ("kimi") and its canonical ("kimi-coding").
    kimi_rows = [s for s in slugs if s in {"kimi", "kimi-coding"}]
    assert kimi_rows == ["kimi-coding"], (
        f"expected a single canonical Kimi row, got: {slugs}"
    )
    assert slugs.count("kimi-coding") == 1
    assert "kimi" not in slugs


def test_distinct_kimi_china_credential_still_listed(monkeypatch):
    """A separate China (kimi-coding-cn) credential remains its own row.

    Negative-control guard: the de-dup must collapse only the alias/canonical
    pair that share a credential, not legitimately distinct providers.
    """
    import hermes_cli.models as hm

    _stub_kimi_discovery(
        monkeypatch,
        canonical=[
            hm.ProviderEntry("kimi-coding", "Kimi / Kimi Coding Plan", "desc"),
            hm.ProviderEntry("kimi-coding-cn", "Kimi / Moonshot (China)", "desc"),
        ],
    )
    monkeypatch.setenv("KIMI_API_KEY", "sk-test-kimi")
    monkeypatch.setenv("KIMI_CN_API_KEY", "sk-test-kimi-cn")

    rows = model_switch.list_authenticated_providers(max_models=10)
    slugs = [r["slug"] for r in rows]

    assert "kimi-coding" in slugs       # canonical global row
    assert slugs.count("kimi-coding") == 1
    assert "kimi" not in slugs          # alias collapsed into the canonical row
    assert "kimi-coding-cn" in slugs    # distinct China endpoint preserved
