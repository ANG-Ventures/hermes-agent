"""`hermes config set`-style toggle for the native content slimmer (PRD #1.5 Phase 5).

Thin wrapper over the existing config surface — maps a single mode word to the
right plugin-block keys so flipping the slimmer is one command, not YAML surgery:

    off    → plugins.native_content_slimmer.enabled = false
    shadow → enabled = true,  mode = shadow
    active → enabled = true,  mode = active_lossless

Optional --allow-tools / --deny-tools pass through to the existing
allow_tools / deny_tools keys.

This is a CONFIG-FILE write only (a plugin block), NOT a harness/runtime change —
it does not touch Hermes model/provider/runtime settings, so it is outside the
privileged config gate. It still surfaces the resulting diff before writing.

Usage:
    python -m plugins.native_content_slimmer.toggle <off|shadow|active>
        [--allow-tools a,b] [--deny-tools c,d] [--yes]
"""

from __future__ import annotations

import argparse
import copy
import sys
from typing import Any, Mapping

PLUGIN_SECTION = "native_content_slimmer"

_MODE_MAP = {
    "off": {"enabled": False},
    "shadow": {"enabled": True, "mode": "shadow"},
    "active": {"enabled": True, "mode": "active_lossless"},
}


def apply_toggle(
    mode: str,
    config: Mapping[str, Any],
    *,
    allow_tools: list[str] | None = None,
    deny_tools: list[str] | None = None,
) -> dict[str, Any]:
    """Return a NEW config dict with the slimmer plugin block toggled.

    Pure function (no I/O) so it is unit-testable and round-trips through
    ``load_slimmer_config``. Raises ValueError on an invalid mode (fails closed).
    """

    if mode not in _MODE_MAP:
        raise ValueError(f"invalid mode {mode!r}; expected one of {sorted(_MODE_MAP)}")

    new_config = copy.deepcopy(dict(config))
    plugins = new_config.setdefault("plugins", {})
    if not isinstance(plugins, dict):
        raise ValueError("config.plugins is not a mapping; refusing to overwrite")
    block = plugins.setdefault(PLUGIN_SECTION, {})
    if not isinstance(block, dict):
        block = {}
        plugins[PLUGIN_SECTION] = block

    block.update(_MODE_MAP[mode])
    if allow_tools is not None:
        block["allow_tools"] = list(allow_tools)
    if deny_tools is not None:
        block["deny_tools"] = list(deny_tools)
    return new_config


def diff_block(old: Mapping[str, Any], new: Mapping[str, Any]) -> str:
    """Human-readable diff of just the plugin block (what changes on disk)."""

    def _block(cfg: Mapping[str, Any]) -> dict:
        p = cfg.get("plugins")
        if isinstance(p, Mapping):
            b = p.get(PLUGIN_SECTION)
            if isinstance(b, Mapping):
                return dict(b)
        return {}

    ob, nb = _block(old), _block(new)
    keys = sorted(set(ob) | set(nb))
    lines = [f"plugins.{PLUGIN_SECTION}:"]
    for k in keys:
        ov, nv = ob.get(k, "<unset>"), nb.get(k, "<unset>")
        marker = "  " if ov == nv else "→ "
        if ov == nv:
            lines.append(f"    {k}: {nv}")
        else:
            lines.append(f"  {marker}{k}: {ov}  =>  {nv}")
    return "\n".join(lines)


def _csv(value: str | None) -> list[str] | None:
    if value is None:
        return None
    return [v.strip() for v in value.split(",") if v.strip()]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Toggle the native content slimmer mode.")
    ap.add_argument("mode", choices=sorted(_MODE_MAP))
    ap.add_argument("--allow-tools", default=None, help="comma-separated allow list")
    ap.add_argument("--deny-tools", default=None, help="comma-separated deny list")
    ap.add_argument("--yes", action="store_true", help="apply without interactive confirm")
    args = ap.parse_args(argv)

    from hermes_cli.config import load_config, save_config, get_config_path
    import yaml

    # operate on the RAW user config (not merged defaults) so we don't dump
    # every default back to disk.
    config_path = get_config_path()
    raw: dict[str, Any] = {}
    if config_path.exists():
        with open(config_path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

    new_config = apply_toggle(
        args.mode, raw,
        allow_tools=_csv(args.allow_tools),
        deny_tools=_csv(args.deny_tools),
    )

    print(diff_block(raw, new_config))
    if not args.yes:
        resp = input("apply this change? [y/N] ").strip().lower()
        if resp not in {"y", "yes"}:
            print("aborted — no change written")
            return 1

    save_config(new_config)
    # confirm round-trip
    from plugins.native_content_slimmer.config import load_slimmer_config

    cfg = load_slimmer_config(load_config())
    print(f"✓ native_content_slimmer: enabled={cfg.enabled} mode={cfg.mode}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
