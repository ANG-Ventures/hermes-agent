"""Spawn identity probe: the worker's OWN terminal prints the identity it will commit with (t_db46f06b).

The dispatcher runs this module as a child with the exact env, argv profile and placement it is about to hand the
worker. It boots the way ``hermes -p <profile> --cli`` does before any tool runs (profile override at import of
``hermes_cli.main``, agent markers, ``agent.process_env_files``), builds the terminal backend the worker's
``terminal`` tool would build from ``_get_env_config()`` (placement re-applied, ``resolve_passthrough_env`` forwarding,
login shell), and runs ``git var`` in the card workspace. There is no second model of the worker's environment: the
emails printed ARE the identity the worker's commits will carry.

``--without-profile-git`` drops the git identity names from the passthrough set, so the same shell shows what the
HOST alone resolves; the dispatcher runs it only after a refusal, to tell a host fault from a profile fault.

Output: one line ``@@hermes-ident-probe <json>`` on stdout; anything else (boot noise) is ignored by the reader.
"""

from __future__ import annotations

import argparse
import json
import re
import sys

MARK = "@@hermes-ident-probe "
# The names that can change what `git var` prints.
IDENTITY_NAME_RE = re.compile(r"(GIT_[A-Z0-9_]+|EMAIL)")
# Reported by value so a profile refusal can name its variable. Emails, never credentials.
_EMAIL_NAMES = ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL", "EMAIL")
# Read in the workspace: repo-local config and ``includeIf "gitdir:..."`` resolve by directory. A fresh workspace
# gets a throwaway repo under it (removed), so a gitdir: condition sees the path the worker's clones will have.
_SCRIPT = ('p=.; git rev-parse --git-dir >/dev/null 2>&1 || { p=$(mktemp -d ./.hermes-ident-probe.XXXXXX '
           '2>/dev/null) && git init -q -- "$p" >/dev/null 2>&1 || { [ -n "$p" ] && rm -rf -- "$p"; p=.; }; }; '
           '( cd "$p" && git var GIT_AUTHOR_IDENT && git var GIT_COMMITTER_IDENT ); rc=$?; '
           '[ "$p" = . ] || rm -rf -- "$p"; echo "@@RC=$rc"')
_IDENT_RE = re.compile(r"<([^>]*)>\s+\d+\s+[-+]\d{4}\s*$")


def parse_output(output: str):
    """Author + committer emails, ``[""]`` when git itself refused (no identity), None when unread."""
    lines = [ln.strip() for ln in (output or "").splitlines() if ln.strip()]
    rc = next((ln[5:] for ln in reversed(lines) if ln.startswith("@@RC=")), None)
    if rc == "128":
        return [""]
    if rc != "0":
        return None
    emails = [m.group(1) for ln in lines for m in [_IDENT_RE.search(ln)] if m]
    return emails if len(emails) == 2 else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True)
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--without-profile-git", action="store_true")
    ap.add_argument("--timeout", type=int, default=30)
    a = ap.parse_args(argv)
    # Boot as the worker does: hermes_cli.main applies `-p` at import, then main() runs these three first.
    sys.argv = ["hermes", "-p", a.profile, "--cli"]
    import hermes_cli.main as vmain
    vmain._capture_kanban_placement()
    vmain._advertise_agent_env()
    vmain._apply_process_env_files()

    from tools import env_passthrough
    real_passthrough = env_passthrough.get_all_passthrough
    if a.without_profile_git:
        env_passthrough.get_all_passthrough = lambda: frozenset(
            n for n in real_passthrough() if not IDENTITY_NAME_RE.fullmatch(n))
    from tools.environments import ssh as _ssh
    from tools.environments.remote_common import resolve_passthrough_env
    from tools.terminal_tool import _get_env_config
    from tools.terminal_tool_backends import _create_environment, _ssh_config_from_config

    cfg = _get_env_config()
    out = {"env_type": cfg["env_type"], "emails": None, "forwarded": {}}
    if cfg["env_type"] == "ssh":
        values, _unset = resolve_passthrough_env(explicit_forward=_ssh._placed_lease_env_names(),
                                                 hermes_env_loader=_ssh._load_hermes_env_vars)
        out["forwarded"] = {n: (values[n] if n in _EMAIL_NAMES else "") for n in sorted(values)
                            if IDENTITY_NAME_RE.fullmatch(n)}
        env = _create_environment("ssh", "", a.workspace, a.timeout, ssh_config=_ssh_config_from_config(cfg),
                                  task_id="ident-probe")
        try:
            res = env.execute(_SCRIPT, cwd=a.workspace, timeout=a.timeout)
        finally:
            env.cleanup()
        out["emails"] = parse_output(res.get("output") or "")
    print(MARK + json.dumps(out, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
