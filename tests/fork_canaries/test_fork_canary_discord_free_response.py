"""Fork canary: Discord free-response channels + the quoted-mention exemption.

Surface: the Discord platform adapter
(``plugins/platforms/discord/adapter.py``), gating path in ``on_message``.

Two fork behaviors, both easy to lose in a parity merge because they live as
small conditions inside a very large gating block:

1. **Scalar coercion.** YAML parses a bare
   ``free_response_channels: 1491973769726791812`` as an ``int``. An
   ``isinstance(raw, str)`` guard silently returns an empty set, so the channel
   stops being free-response and the bot goes mute for every unmentioned
   message — with no error anywhere. ``_discord_free_response_channels`` must
   coerce any scalar via ``str()`` before splitting.

2. **Quoted-bot-mention suppression.** Free-response channels routinely contain
   *quoted* mentions of other bots. The fork answers those anyway and suppresses
   only a message that *starts* with another bot's mention. That half is tested
   end-to-end at the adapter's admission gate in
   tests/gateway/test_discord_quoted_mention_admission.py; this file covers the
   coercion half only.

Adapter-level, no live gateway, no Discord connection.
"""

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


def _ensure_discord_mock():
    """Minimal ``discord`` stub so the adapter module imports headlessly.

    Mirrors the pattern used by tests/gateway/conftest.py and the sibling
    telegram tests: only what the module needs at import time.
    """
    if "discord" in sys.modules and isinstance(
        getattr(sys.modules["discord"], "__file__", None), str
    ):
        return
    mod = MagicMock()
    for name in ("discord", "discord.ext", "discord.ext.commands", "discord.abc"):
        sys.modules.setdefault(name, mod)


_ensure_discord_mock()

from gateway.config import PlatformConfig  # noqa: E402


def _adapter(extra=None, env=None):
    """Build a DiscordAdapter shell without running __init__ / connecting.

    Only the two attributes the channel resolver reads are populated
    (``config`` and the ``_gate_env`` env fallback), matching the
    ``TelegramAdapter.__new__`` shell used by
    tests/gateway/test_telegram_intake_sentinel.py.
    """
    from plugins.platforms.discord.adapter import DiscordAdapter

    adapter = DiscordAdapter.__new__(DiscordAdapter)
    adapter.config = PlatformConfig(enabled=True, token="***", extra=extra or {})
    env = env or {}
    adapter._gate_env = lambda key: env.get(key)
    return adapter


# --------------------------------------------------------------------------- #
# 1. Scalar / list / CSV coercion
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "raw,expected",
    [
        # Bare YAML int — the regression this coercion exists for.
        (1491973769726791812, {"1491973769726791812"}),
        # String scalar.
        ("1491973769726791812", {"1491973769726791812"}),
        # CSV string with padding.
        (" 111 , 222 ,333 ", {"111", "222", "333"}),
        # YAML list, mixed types.
        ([111, "222", " 333 "], {"111", "222", "333"}),
        # Wildcard survives in both shapes so callers can short-circuit.
        ("*", {"*"}),
        (["*"], {"*"}),
    ],
)
def test_free_response_channels_coerces_every_config_shape(raw, expected):
    """RED-PROVABLE: in ``_discord_free_response_channels``
    (plugins/platforms/discord/adapter.py ~L6751) replace
    ``s = str(raw).strip() if raw is not None else ""`` with
    ``s = raw.strip() if isinstance(raw, str) else ""`` — the bare-int and
    YAML-list-of-ints cases collapse to an empty set and fail."""
    adapter = _adapter(extra={"free_response_channels": raw})
    assert adapter._discord_free_response_channels() == expected, (
        f"free_response_channels={raw!r} did not resolve to {expected!r}; "
        f"the channel silently stops being free-response and the bot goes mute."
    )


def test_free_response_channels_falls_back_to_env():
    """The env var is the documented fallback when the config key is absent.

    RED-PROVABLE: delete the ``if raw is None: raw = self._gate_env(
    "DISCORD_FREE_RESPONSE_CHANNELS")`` branch (adapter.py ~L6741)."""
    adapter = _adapter(extra={}, env={"DISCORD_FREE_RESPONSE_CHANNELS": "42,43"})
    assert adapter._discord_free_response_channels() == {"42", "43"}


def test_absent_config_yields_empty_set_not_wildcard():
    """Fail-CLOSED on absence: no config must never mean "every channel".

    RED-PROVABLE: change the final ``return set()`` in
    ``_discord_free_response_channels`` (adapter.py ~L6754) to
    ``return {"*"}``."""
    assert _adapter(extra={}).\
        _discord_free_response_channels() == set()


# --------------------------------------------------------------------------- #
# 2. Quoted-mention exemption: driven at the real gate, not replicated here.
#    tests/gateway/test_discord_quoted_mention_admission.py sends fake messages through
#    DiscordAdapter._dispatch_incoming_message and asserts dispatch vs suppression
#    (t_2206d24f; registered as this entry's call_site_tests in fork-features.json).
# --------------------------------------------------------------------------- #

def test_wildcard_membership_short_circuits_channel_matching():
    """``"*"`` is preserved in the set precisely so the caller can
    short-circuit; if it were expanded or dropped, the intersection against
    numeric channel keys would always be empty and every channel would fail
    the free-response check.

    RED-PROVABLE: make ``_discord_free_response_channels`` strip ``"*"`` from
    its return value (adapter.py ~L6747) — the wildcard verdict flips."""
    free = _adapter(extra={"free_response_channels": "*"}).\
        _discord_free_response_channels()
    channel_keys = {"1491973769726791812", "#general"}
    is_free = "*" in free or bool(channel_keys & free)
    assert is_free, "wildcard free_response_channels stopped matching any channel"
