from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path

from scripts.refactor_equiv.sandbox_guard import require_sandboxed_home


def run_case(case: dict):
    kind = case["kind"]
    if kind == "placeholders":
        helper = _helper("_sql_placeholders")
        return {
            "return": [helper(values) for values in case["values"]],
            "messages": [],
            "db": [],
        }
    if kind == "denorm_flag":
        return _run_denorm_case(case)
    raise AssertionError(f"unknown state_ext case kind: {kind!r}")


def _run_denorm_case(case: dict):
    helper = _helper("_session_list_denorm_enabled")
    out = []
    with _patched_env(case.get("env") or {}):
        if "config_error" in case:
            import hermes_cli.config as config

            old_read_raw_config = config.read_raw_config
            try:
                config.read_raw_config = lambda: (_ for _ in ()).throw(
                    RuntimeError(case["config_error"])
                )
                for enabled in case["configs"]:
                    _write_dashboard_flag(bool(enabled))
                    out.append(helper())
            finally:
                config.read_raw_config = old_read_raw_config
        else:
            for enabled in case["configs"]:
                _write_dashboard_flag(bool(enabled))
                out.append(helper())
    return {"return": out, "messages": [], "db": []}


def _helper(name: str):
    # Post-extraction tree: the symbols live in hermes_state_ext.
    # Pre-extraction replay (the throwaway-worktree PRE-GREEN check): the module
    # doesn't exist yet and the symbols still live inline in hermes_state — that
    # branch is the reason this fallback exists, NOT dead code. hermes_state is
    # imported lazily here because on a post-extraction tree it itself imports
    # hermes_state_ext (a top-level import would raise before this fallback ran).
    try:
        import hermes_state_ext
    except ModuleNotFoundError:
        import hermes_state
        return getattr(hermes_state, name)
    return getattr(hermes_state_ext, name)


def _write_dashboard_flag(enabled: bool) -> None:
    config_path = require_sandboxed_home() / "config.yaml"
    config_path.write_text(
        "dashboard:\n"
        f"  session_list_denorm: {json.dumps(enabled)}\n",
        encoding="utf-8",
    )


@contextmanager
def _patched_env(values: dict[str, str]):
    tracked = set(values) | {"HERMES_SESSION_LIST_DENORM"}
    old = {key: os.environ.get(key) for key in tracked}
    try:
        for key, value in values.items():
            os.environ[key] = str(value)
        if "HERMES_SESSION_LIST_DENORM" not in values:
            os.environ.pop("HERMES_SESSION_LIST_DENORM", None)
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
