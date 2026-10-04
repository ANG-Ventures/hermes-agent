"""The ``/overview`` gateway command: this chat's session card overview, self-serve.

The report is built by the fleet's ``scripts/session-overview.py`` (the ONE overview tool:
board census, live PR/deploy probes, header accounting, what-flips-it groups). This command
only resolves the invoking chat's session id, runs that script with ``--lineage`` (every
session id of the chat counts), and returns its Discord-shaped text. The gateway adapter
splits a reply longer than the platform cap.

Kept out of ``gateway.slash_commands`` so it is testable without a gateway runner.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

SCRIPT_REL = Path("scripts") / "session-overview.py"
# A full census with live GitHub + deploy readbacks measured 25 s on 939 cards (2026-10-03).
TIMEOUT_S = 240
# session-overview.py's stderr line for an empty census (exit 2): "no born-here cards for <ids>"
NO_CARDS_MARK = "no born-here cards for"


def overview_script(root: Optional[Path] = None) -> Path:
    if root is None:
        from hermes_constants import get_default_hermes_root

        root = get_default_hermes_root()
    return Path(root) / SCRIPT_REL


def build_argv(session_id: str, args: str, script: Path, python: str) -> list[str]:
    """argv for the script. ``/overview fast`` skips the GitHub/deploy reads (board numbers only)."""
    argv = [python, str(script), session_id, "--lineage"]
    words = (args or "").split()
    if "fast" in words or "--no-network" in words:
        argv.append("--no-network")
    return argv


def render_overview(
    session_id: Optional[str],
    args: str = "",
    *,
    root: Optional[Path] = None,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    python: Optional[str] = None,
    timeout: int = TIMEOUT_S,
) -> str:
    """Reply text for ``/overview``. Never raises: every failure is a one-line reason."""
    if not session_id:
        return ("/overview: this chat has no session yet, so there are no session cards. "
                "Send any message to open the session, then retry.")
    script = overview_script(root)
    if not script.is_file():
        return f"/overview: {script} is not installed on this host."
    argv = build_argv(session_id, args, script, python or sys.executable)
    try:
        p = run(argv, capture_output=True, text=True, errors="replace", timeout=timeout,
                stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return f"/overview: session-overview.py did not finish in {timeout} s; try `/overview fast`."
    except OSError as exc:
        return f"/overview: could not run session-overview.py ({type(exc).__name__}: {exc})"
    out = (p.stdout or "").strip()
    # rc=2 is also argparse's usage-error code: only the script's own empty-census line means "no cards"
    if p.returncode == 2 and not out and NO_CARDS_MARK in (p.stderr or ""):
        return f"/overview: no cards were born in this chat's sessions ({session_id} and its lineage)."
    if p.returncode != 0 or not out:
        err = ((p.stderr or "").strip().splitlines() or [f"rc={p.returncode}"])[-1][:300]
        logger.warning("/overview failed rc=%s: %s", p.returncode, err)
        return f"/overview: session-overview.py failed (rc={p.returncode}): {err}"
    return out
