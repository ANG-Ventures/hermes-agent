"""The WRITE side of the phantom-session class: canonicalize before persisting.

#682 fixed the two READERS (the wake path's lane comparison and notify-repair's
evidence lookup). It deployed clean — and four freshly-drifted
``kanban_notify_subs`` rows appeared **two minutes later**, because the WRITERS
were still persisting the raw envelope spelling.

A Discord guild chat is keyed ``group`` by ``build_session_key``. A row stored
as ``chat_type='channel'`` therefore cannot match its own chat's routing entry;
the wake finds no identity, keys a bare ``group:<chat>`` session, and mints the
phantom that replies into the user's channel at the config-default model and
reasoning effort.

Fixing only the readers papers over rows that should never have been written:
the reader normalizes at query time, but the stored data stays wrong for every
other consumer (dashboards, exports, future code). Reading and writing must
agree on the spelling.

Non-Discord platforms are untouched — ``canonical_chat_type`` is a no-op for
them, and Teams / Telegram / HomeAssistant own ``channel`` as a real type.
"""
import ast
from pathlib import Path

import pytest

from gateway.routing_identity import canonical_chat_type

ROOT = Path(__file__).resolve().parents[2]

#: Every site that persists a chat_type onto a notify subscription.
WRITE_SITES = (
    ("tools/kanban_tools.py", "_subscribe_calling_session"),
    ("gateway/slash_commands.py", None),  # kanban slash-command subscribe
)


def test_discord_channel_is_canonicalized_to_group():
    assert canonical_chat_type("discord", "channel") == "group"
    assert canonical_chat_type("discord", "group") == "group"


@pytest.mark.parametrize("platform", ["teams", "telegram", "homeassistant"])
def test_real_channel_platforms_keep_their_type(platform):
    """Negative control: a fix that rewrote every platform would re-lane three."""
    assert canonical_chat_type(platform, "channel") == "channel"


@pytest.mark.parametrize("platform", ["discord", "teams", "telegram"])
def test_dm_is_never_rewritten(platform):
    """Negative control: canonicalization must not collapse dm into group."""
    assert canonical_chat_type(platform, "dm") == "dm"


def _calls_canonicalizer(path: Path) -> bool:
    """Whether the module invokes canonical_chat_type anywhere."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name == "canonical_chat_type":
                return True
    return False


@pytest.mark.parametrize("relative,_owner", WRITE_SITES)
def test_every_notify_sub_writer_canonicalizes(relative, _owner):
    """LINT: a writer that persists a raw chat_type re-creates the bug class.

    Gated on the file actually calling add_notify_sub, so an unrelated module
    is never flagged and the rule cannot pass vacuously if the writer moves.
    """
    path = ROOT / relative
    source = path.read_text(encoding="utf-8")
    assert "add_notify_sub" in source, (
        f"{relative} no longer writes notify subs — this lint is pointed at a "
        "file that moved; re-derive WRITE_SITES rather than deleting the case"
    )
    assert _calls_canonicalizer(path), (
        f"{relative} persists a chat_type without canonical_chat_type() — a "
        "Discord guild chat stored as 'channel' cannot match its own routing "
        "entry and will mint a phantom session (2026-09-12)"
    )


def test_slash_command_chat_type_is_not_trapped_in_the_metadata_branch():
    """Regression: chat_type was only assigned when metadata was a dict.

    The subscription could therefore be written with chat_type unbound/unset
    whenever _thread_metadata_for_source returned None — a second way for the
    same row to end up wrong.
    """
    source = (ROOT / "gateway/slash_commands.py").read_text(encoding="utf-8")
    assign = source.index('chat_type = str(getattr(source, "chat_type"')
    branch = source.index("if isinstance(delivery_metadata, dict):", assign - 2000)
    assert assign < branch, (
        "chat_type must be resolved BEFORE the delivery_metadata branch, or a "
        "subscription written with metadata=None carries no chat_type at all"
    )
