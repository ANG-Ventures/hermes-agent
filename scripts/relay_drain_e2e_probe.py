#!/usr/bin/env python3
"""Relay deploy-drain E2E probe (t_df1df8bb): the regression instrument for the
claude-pool deploy-drain contract (claude-pool t_826861ab / t_ef5f9f1e) and the
harness that waits it out (fork #1572, t_4349cf26).

One run = one real turn on ONE worker seat, served by a SCRATCH claude-pool bpr
relay (the deployed relay code, its own port/cache-dir/log, a one-seat registry).
The live relays, the live runtime tree and every gateway are never touched: a
drain on the live bpr relay also hits every pre-fix gateway on it, which would
announce a false failover to a real chat while the probe runs.

  drain   : deploy-drain the scratch relay (ttl --drain-ttl) --drain-delay s after
            the turn starts, deploy-undrain at --undrain-at s.
  restart : same drain, then SIGTERM the scratch relay --restart-offset s after
            the turn logs its first drain 503 and relaunch it --restart-gap s
            after it exits (a launchd KeepAlive relaunch in miniature), so the
            next retry lands in the listener gap. The new process starts undrained.

Verdict (exit 0 = PASS, 1 = FAIL, 2 = INCONCLUSIVE/setup error):
  PASS  = the relay refused >= 1 call with the drain 503, the turn completed,
          state/model-route-changes.log has no failover row, the fallback_events
          ledger has no row, and stdout/stderr carry no failover announce.
  --expect-failover inverts the route assertions (the pre-fix negative control).

Arms (operator, on the Studio; each drain arm = at most one served call on --seat):
  AFTER  python3 scripts/relay_drain_e2e_probe.py --runtime-tree ~/.hermes/runtime/hermes-agent \
             --out /tmp/drain-after --mode drain             # waits the drain out on fable
  RESTART  ... --mode restart                                 # drain, SIGTERM, relaunch gap
  BEFORE ... --runtime-tree <clone at the parent of #1572, venv symlinked> \
             --hold-drain --expect-failover [--relay-v2 off]  # failover; 0 served calls
  BOUND  ... --hold-drain --relay-drain-wait-s 40             # bound spent: same-provider skip
             (exits 1 by design: a held drain cannot complete; read the report)

Reference run 2026-09-30 18:12-18:28 PT (t_df1df8bb, seat sub-vps-13, relay 4eb9630):
  AFTER    2x 503 draining-for-deploy (v2 class/hop/cause), 2x 15 s wait on fable,
           served fable, 0 route-change rows, 0 announces.
  RESTART  503 -> wait -> APIConnectionError in the relaunch gap -> "local relay
           back after 5.0s" -> served fable on the new relay pid; 0 rows.
  BEFORE   2x 503 on fable -> "Fallback activated: fable -> opus (claude-bpr)",
           1 route-change failover row, 1 fallback_events row; v2 off renders
           "(provider overloaded) ... unclassified error (hop unknown, sub unknown)",
           v2 on renders "(relay busy) ... relay busy: all subs at capacity (at the relay)".
  BOUND    4x 503 waited on fable, "Fallback skip: claude-bpr/claude-opus-5-5 is on
           the relay that is draining", 0 rows.
Registers nothing on a schedule: it is an operator instrument for drain-contract changes.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HOME_ENV = "HERMES" + "_HOME"  # the agent's home env var
USER_HOME = Path.home()
DEPLOY_RELAY = USER_HOME / ".hermes/deploy/claude-pool/claude_pool_relay.py"
DEPLOY_RELAY_PY = USER_HOME / ".hermes/deploy/venvs/claude-pool/bin/python3"
PROD_REGISTRY = USER_HOME / ".hermes/config/claude-subs.json"
PROD_ENV = USER_HOME / ".hermes/.env"
PROD_PLUGINS = USER_HOME / ".hermes/plugins"
# The relay resolves its router/pool config under the home env var. Copy the
# LIVE files so the scratch relay boots with the live flags (error_class_v2 on),
# not the defaults it falls back to under a worker's profile home.
RELAY_HOME_FILES = ("config/claude-router.json", "config/claude-pool.json")
RELAY_HOME_LINKS = ("var/subs-portal/site/subs.json",)
# Relay ports in use on the Studio (claude_pool_relay FOREIGN_STUDIO_PORTS +
# 18810/18811/18816 relays, measured 2026-09-30). 18897 was free.
DEFAULT_PORT = 18897
# Never bind or admin a production relay port (apr 18810, bpr 18811, dlr 18816,
# apx-0 18801, cliproxyapi 18812/18813, caddy 18814, 18820).
REFUSED_PORTS = frozenset({18801, 18810, 18811, 18812, 18813, 18814, 18816, 18820})
# --out is only ever deleted when it carries this marker (written at creation).
OUT_MARKER = ".relay-drain-probe-out"
# The drain refusal body token (claude_pool_relay.DEPLOY_DRAIN_ERROR): the only
# accepted evidence that a turn met the drain, never a bare 503.
DRAIN_TOKEN = "draining-for-deploy"
PROMPT = (
    "Reply with exactly the text DRAIN-PROBE-OK and nothing else. Do not use tools."
)
ANNOUNCE_RX = re.compile(
    r"Fallback activated|provider overloaded|unclassified error|fell back to|switched to",
    re.I,
)
SUB0_KEYS = {"local", "sub-0", "0"}


def log(msg: str) -> None:
    print(f"[probe {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def http(method: str, url: str, body: dict | None = None, timeout: float = 5.0):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "X-Pool-Admin": "relay-drain-e2e-probe",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def write_private(path: Path, text: str) -> None:
    """Create ``path`` 0600 from the first byte (no write-then-chmod window)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(path, 0o600)


def one_seat_registry(seat_label_prefix: str, dest: Path) -> None:
    d = json.loads(PROD_REGISTRY.read_text(encoding="utf-8-sig"))
    subs = d["subs"]
    keep = [
        s for s in subs if str(s.get("label", "")).split(" ")[0] == seat_label_prefix
    ]
    if len(keep) != 1:
        raise SystemExit(
            f"seat {seat_label_prefix!r}: {len(keep)} registry rows, need 1"
        )
    if seat_label_prefix in SUB0_KEYS or "Mac Studio" in str(keep[0].get("label")):
        raise SystemExit("refusing sub 0 (Ace's own seat)")
    d["subs"] = keep
    write_private(dest, json.dumps(d, indent=2))


class ScratchRelay:
    def __init__(self, port: int, workdir: Path, registry: Path, v2: str = "live"):
        self.port, self.workdir, self.registry = port, workdir, registry
        self.cache = workdir / "relay-cache"
        self.log_path = workdir / "relay.log"
        self.proc: subprocess.Popen | None = None
        self.launches = 0
        self.home = workdir / "relay-home"
        for rel in RELAY_HOME_FILES:
            src = USER_HOME / ".hermes" / rel
            if src.exists():
                (self.home / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, self.home / rel)
        if v2 != "live":
            # error_class_v2 off = the drain 503 as it was before claude-pool #180
            # (bare body, no class/hop/cause): the 2026-09-30 13:03 wire.
            rp = self.home / "config/claude-router.json"
            cfg = json.loads(rp.read_text(encoding="utf-8-sig")) if rp.exists() else {}
            cfg["error_class_v2"] = v2 == "on"
            rp.parent.mkdir(parents=True, exist_ok=True)
            rp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        for rel in RELAY_HOME_LINKS:
            src = USER_HOME / ".hermes" / rel
            if src.exists():
                (self.home / rel).parent.mkdir(parents=True, exist_ok=True)
                (self.home / rel).symlink_to(src)
        self.env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("KANBAN", "HERMES", "HERMES"))
        }
        self.env[HOME_ENV] = str(self.home)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def port_in_use(self) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sk:
            sk.settimeout(1.0)
            return sk.connect_ex(("127.0.0.1", self.port)) == 0

    def _serve_started_by(self, pid: int) -> bool:
        """The listener is ours only once OUR process logged serve_start on our
        port; a healthy endpoint alone can be a foreign relay."""
        return any(
            e.get("event") == "serve_start"
            and e.get("pid") == pid
            and e.get("port") == self.port
            for e in relay_events(self.log_path)
        )

    def start(self, wait_s: float = 60.0) -> None:
        if self.port in REFUSED_PORTS:
            raise SystemExit(f"refusing production relay port {self.port}")
        if self.port_in_use():
            raise SystemExit(
                f"port {self.port} already has a listener; pick a free --port"
            )
        self.launches += 1
        out = open(self.workdir / f"relay.stdout.{self.launches}.log", "ab")
        self.proc = subprocess.Popen(
            [
                str(DEPLOY_RELAY_PY),
                str(DEPLOY_RELAY),
                "--pool",
                "bpr",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
                "--registry",
                str(self.registry),
                "--cache-dir",
                str(self.cache),
                "--log",
                str(self.log_path),
            ],
            cwd=str(DEPLOY_RELAY.parent),
            env=self.env,
            stdout=out,
            stderr=subprocess.STDOUT,
        )
        deadline = time.time() + wait_s
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise SystemExit(
                    f"scratch relay exited rc={self.proc.returncode}; see {out.name}"
                )
            try:
                h = http("GET", self.base + "/health")
                if (
                    h.get("status") == "ok"
                    and h.get("eligible_count", 0) >= 1
                    and self._serve_started_by(self.proc.pid)
                ):
                    log(
                        f"scratch relay up pid={self.proc.pid} eligible={h.get('eligible_keys')}"
                    )
                    return
            except Exception:
                pass
            time.sleep(0.5)
        raise SystemExit("scratch relay never reported an eligible seat")

    def admin(self, action: str, **body) -> dict:
        if self.proc is None or self.proc.poll() is not None:
            raise SystemExit(
                f"{action}: the scratch relay is not running; refusing to admin the port"
            )
        r = http("POST", self.base + f"/admin/pool/{action}", body)
        log(
            f"{action} -> draining={r.get('draining_for_deploy')} inflight={r.get('inflight')}"
        )
        return r

    def stop(self, timeout: float = 90.0) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()


def scratch_home(
    workdir: Path,
    port: int,
    model: str,
    fallback_model: str,
    drain_wait_s: float | None,
) -> Path:
    home = workdir / "home"
    (home / "logs").mkdir(parents=True, exist_ok=True)
    (home / "state").mkdir(parents=True, exist_ok=True)
    cfg = [
        "model:",
        "  provider: claude-bpr",
        f"  default: {model}",
        "fallback_providers:",
        "- provider: claude-bpr",
        f"  model: {fallback_model}",
        "plugins:",
        "  enabled:",
        "    - blackbox",
        "  disabled: []",
        # Blackbox is the fallback_events ledger; without its block it records nothing.
        "blackbox:",
        "  enabled: true",
        "  alerts_enabled: false",
        # oneshot resolves tools via platform_toolsets.cli (default hermes-cli);
        # an explicit empty list is the "no tools" selection. tools_preflight()
        # proves it before any turn.
        "platform_toolsets:",
        "  cli: []",
        "memory:",
        "  memory_enabled: false",
        "  user_profile_enabled: false",
    ]
    if drain_wait_s is not None:
        cfg += ["fallback:", f"  relay_drain_wait_s: {drain_wait_s}"]
    (home / "config.yaml").write_text("\n".join(cfg) + "\n", encoding="utf-8")
    # Only the relay bearer is copied (0600, scratch, never printed or committed).
    key = [
        ln
        for ln in PROD_ENV.read_text(encoding="utf-8-sig").splitlines()
        if ln.startswith("CLAUDE_BPP_KEY=")
    ]
    write_private(
        home / ".env",
        "\n".join(key + [f"CLAUDE_BPP_BASE_URL=http://127.0.0.1:{port}/v1"]) + "\n",
    )
    (home / "plugins").symlink_to(PROD_PLUGINS)
    return home


def agent_env(home: Path, tree: Path, port: int) -> dict:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("KANBAN", "HERMES", "HERMES"))
    }
    env[HOME_ENV] = str(home)
    env["CLAUDE_BPP_BASE_URL"] = f"http://127.0.0.1:{port}/v1"
    env["PYTHONPATH"] = str(tree)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def tree_python(tree: Path) -> Path:
    py = tree / "venv" / "bin" / "python"
    if not py.exists():
        raise SystemExit(f"{py} missing (symlink the runtime venv into the tree)")
    return py


def tools_preflight(tree: Path, env: dict, cwd: Path) -> None:
    """Resolve the tool schema exactly as oneshot does; refuse unless empty."""
    code = (
        "import json;"
        "from hermes_cli.config import load_config;"
        "from hermes_cli.tools_config import _get_platform_tools;"
        "from model_tools import get_tool_definitions;"
        "ts = sorted(_get_platform_tools(load_config(), 'cli'));"
        "d = get_tool_definitions(enabled_toolsets=ts, quiet_mode=True);"
        "print(json.dumps({'toolsets': ts, 'tools': [t['function']['name'] for t in d]}))"
    )
    out = subprocess.run(
        [str(tree_python(tree)), "-c", code],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    if out.returncode:
        raise SystemExit(f"tools preflight failed: {out.stderr[-800:]}")
    got = json.loads(out.stdout.strip().splitlines()[-1])
    if got["tools"]:
        raise SystemExit(
            f"tools preflight: the probe turn would expose {len(got['tools'])} tools "
            f"({got['tools'][:8]}...); refusing"
        )
    log(f"tools preflight: toolsets={got['toolsets']} tools=0")


def confirm_tree(tree: Path, env: dict, cwd: Path) -> str:
    code = (
        "import agent.error_classifier as m, agent.conversation_loop as c;"
        "print(m.__file__);print(c.__file__);"
        "print('relay_draining' in m.FailoverReason.__members__)"
    )
    out = subprocess.run(
        [str(tree_python(tree)), "-c", code],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    if out.returncode:
        raise SystemExit(f"tree import failed: {out.stderr[-800:]}")
    lines = out.stdout.split()
    for f in lines[:2]:
        if not os.path.realpath(f).startswith(os.path.realpath(tree)):
            raise SystemExit(f"module resolved OUTSIDE the tree: {f}")
    log(f"tree modules: {lines[0]} | relay_draining in classifier: {lines[2]}")
    return lines[2]


def relay_events(path: Path) -> list[dict]:
    rows = []
    if path.exists():
        for ln in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            i = ln.find("{")
            if i < 0:
                continue
            try:
                row = json.loads(ln[i:])
            except Exception:
                continue
            row["_ts"] = ln[:i].strip()
            rows.append(row)
    return rows


def ledger_rows(home: Path) -> tuple[list[tuple], list[tuple]]:
    """(turn_api_calls rows, fallback_events rows). A recorded api call proves
    the Blackbox ledger was live for this run, so an empty fallback_events is a
    real zero, not a dead instrument."""
    db = home / "blackbox" / "turns.db"
    if not db.exists():
        return [], [("<no turns.db: blackbox did not record>",)]
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        calls = con.execute(
            "select provider, model, http_status, sub_key from turn_api_calls "
            "order by ts"
        ).fetchall()
        return calls, con.execute(
            "select kind, from_provider, from_model, to_provider, to_model, "
            "reason, trigger_class, http_status, notice_text from fallback_events "
            "order by id"
        ).fetchall()
    except sqlite3.OperationalError as e:
        return [], [(f"<{e}>",)]
    finally:
        con.close()


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--runtime-tree", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--mode", choices=("drain", "restart"), default="drain")
    ap.add_argument(
        "--seat",
        default="sub-vps-13",
        help="the ONE worker seat the scratch relay serves",
    )
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--model", default="claude-fable-5-1")
    ap.add_argument("--fallback-model", default="claude-opus-5-5")
    # Drain at t=0: the relay refuses only NEW turns, so a drain that lands after
    # the turn's first call reached the relay measures nothing (live 14:08/15:08
    # windows had inflight 0 for the same reason).
    ap.add_argument("--drain-delay", type=float, default=0.0)
    # One-shot CLI startup -> first chat POST measured 18 s (18:12:36 -> 18:12:54,
    # 2026-09-30), so a 15 s window from t=0 closes before the turn reaches it.
    # Undrain at 40 s = startup + ~one Retry-After 15 s cycle inside the drain;
    # TTL 60 s is the abandoned-probe safety (relay clamps 10..600).
    ap.add_argument("--drain-ttl", type=int, default=60, help="relay clamps to 10..600")
    ap.add_argument("--undrain-at", type=float, default=40.0)
    # restart mode: SIGTERM the relay --restart-offset s after the turn logs its
    # first drain 503, so the next Retry-After (15 s) retry lands inside the
    # relaunch gap and exercises the loopback-restart wait, not just the drain.
    ap.add_argument("--restart-offset", type=float, default=10.0)
    ap.add_argument(
        "--restart-gap",
        type=float,
        default=8.0,
        help="relaunch delay; > Retry-After - offset so a retry hits the gap",
    )
    ap.add_argument(
        "--relay-drain-wait-s",
        type=float,
        default=None,
        help="override fallback.relay_drain_wait_s in the scratch home",
    )
    ap.add_argument("--turn-timeout", type=float, default=600.0)
    # Hold the drain for the whole turn (undrain only after it ends). On a
    # pre-fix tree the fable->opus failover lands on the same draining relay, so
    # the BEFORE number costs zero served calls. On a fixed tree the turn waits
    # fallback.relay_drain_wait_s (150 s) and then fails with no same-provider
    # fallback: use --relay-drain-wait-s to shorten it.
    ap.add_argument("--hold-drain", action="store_true")
    ap.add_argument(
        "--relay-v2",
        choices=("live", "on", "off"),
        default="live",
        help="scratch relay error_class_v2: live config, or forced on/off",
    )
    ap.add_argument(
        "--expect-failover", action="store_true", help="negative control (pre-fix tree)"
    )
    a = ap.parse_args()
    if a.hold_drain:
        # The relay clamps a drain TTL to 600 s and an active drain is not
        # renewable without an undrain gap, so a held drain must outlast the turn.
        if a.turn_timeout + 60 > 600:
            raise SystemExit(
                "--hold-drain needs --turn-timeout <= 540 (relay drain TTL cap 600 s)"
            )
        a.drain_ttl = max(a.drain_ttl, int(a.turn_timeout + 60))

    tree = a.runtime_tree.resolve()
    live = (USER_HOME / ".hermes/runtime/hermes-agent").resolve()
    work = a.out.resolve()
    if work.exists():
        if not work.is_dir() or a.out.is_symlink():
            raise SystemExit(
                f"--out {work} exists and is not a plain directory; refusing"
            )
        if any(work.iterdir()) and not (work / OUT_MARKER).is_file():
            raise SystemExit(
                f"--out {work} is non-empty and not a previous probe output "
                f"(no {OUT_MARKER}); refusing to delete it"
            )
        shutil.rmtree(work)
    # 0700 before anything sensitive (relay bearer, registry) lands inside.
    work.mkdir(parents=True, mode=0o700)
    os.chmod(work, 0o700)
    (work / OUT_MARKER).write_text(
        "relay_drain_e2e_probe output dir\n", encoding="utf-8"
    )
    registry = work / "registry.json"
    one_seat_registry(a.seat, registry)
    home = scratch_home(work, a.port, a.model, a.fallback_model, a.relay_drain_wait_s)
    env = agent_env(home, tree, a.port)
    log(
        f"tree={tree} (live runtime tree: {'YES, read-only' if tree == live else 'no'}) home={home}"
    )
    fixed = confirm_tree(tree, env, work)
    tools_preflight(tree, env, work)

    relay = ScratchRelay(a.port, work, registry, v2=a.relay_v2)
    relay.start()
    stdout_p, stderr_p = work / "turn.stdout.log", work / "turn.stderr.log"
    # The real CLI one-shot path. oneshot.run_oneshot() calls
    # logging.disable(CRITICAL), which empties agent.log; the driver no-ops that
    # one call so the drain wait / failover lines land in the scratch agent.log.
    driver = (
        "import logging, sys; logging.disable = lambda *a, **k: None; "
        "sys.argv = ['hermes'] + sys.argv[1:]; "
        "from hermes_cli.main import main; sys.exit(main())"
    )
    usage_p = work / "usage.json"
    cmd = [
        str(tree_python(tree)),
        "-c",
        driver,
        "-z",
        PROMPT,
        "--usage-file",
        str(usage_p),
    ]
    t0 = time.monotonic()
    drained = undrained = restarted = False
    drain_window: dict = {}
    first_503 = None
    stamp = lambda k: drain_window.__setitem__(k, time.strftime("%Y-%m-%d %H:%M:%S"))
    try:
        if a.drain_delay <= 0:
            relay.admin("deploy-drain", ttl_s=a.drain_ttl)
            stamp("drain")
            drained = True
        turn = subprocess.Popen(
            cmd,
            cwd=str(work),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=open(stdout_p, "wb"),
            stderr=open(stderr_p, "wb"),
        )
        log(f"turn started pid={turn.pid} mode={a.mode}")
        while turn.poll() is None:
            el = time.monotonic() - t0
            if el > a.turn_timeout:
                turn.kill()
                log("turn TIMED OUT")
                break
            if not drained and el >= a.drain_delay:
                relay.admin("deploy-drain", ttl_s=a.drain_ttl)
                stamp("drain")
                drained = True
            if (
                a.mode == "drain"
                and not a.hold_drain
                and not undrained
                and el >= a.undrain_at
            ):
                relay.admin("deploy-undrain")
                stamp("undrain")
                undrained = True
            if a.mode == "restart" and first_503 is None:
                al = home / "logs" / "agent.log"
                if al.exists() and re.search(
                    DRAIN_TOKEN,
                    al.read_text(encoding="utf-8-sig", errors="replace"),
                ):
                    first_503 = el
                    log(
                        f"turn met the drain at +{el:.1f}s; SIGTERM at +{el + a.restart_offset:.1f}s"
                    )
            if (
                a.mode == "restart"
                and not restarted
                and first_503 is not None
                and el >= first_503 + a.restart_offset
            ):
                log("SIGTERM scratch relay (graceful drain-then-exit)")
                stamp("sigterm")
                relay.stop()
                log(f"relay exited; relaunch in {a.restart_gap}s")
                time.sleep(a.restart_gap)
                relay.start()
                stamp("relaunched")
                restarted = True
            time.sleep(0.2)
        rc = turn.wait()
        wall = time.monotonic() - t0
        if drained and not undrained and a.mode == "drain":
            relay.admin("deploy-undrain")
            stamp("undrain")
    finally:
        relay.stop()

    out_txt = stdout_p.read_text(encoding="utf-8-sig", errors="replace")
    err_txt = stderr_p.read_text(encoding="utf-8-sig", errors="replace")
    events = relay_events(relay.log_path)
    picks = [e for e in events if e.get("event") == "pick"]
    seats_served = sorted({
        e.get("chosen") for e in events if e.get("event") == "ok" and e.get("chosen")
    })
    models_picked = [e.get("model") for e in picks]
    serve_start = [
        {"pid": e.get("pid"), "error_class_v2": e.get("error_class_v2"), "ts": e["_ts"]}
        for e in events
        if e.get("event") == "serve_start"
    ]
    try:
        usage = json.loads(usage_p.read_text(encoding="utf-8-sig"))
    except Exception:
        usage = None
    route_log = home / "state" / "model-route-changes.log"
    route_rows = (
        route_log.read_text(encoding="utf-8-sig").splitlines() if route_log.exists() else []
    )
    failover_rows = [r for r in route_rows if "failover" in r.lower()]
    ledger_calls, ledger = ledger_rows(home)
    announces = sorted({m.group(0) for m in ANNOUNCE_RX.finditer(out_txt + err_txt)})
    agent_log = home / "logs" / "agent.log"
    agent_lines = (
        agent_log.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        if agent_log.exists()
        else []
    )
    excerpt = [
        ln
        for ln in agent_lines
        if re.search(
            r"drain|Retry-After|retry_after|overloaded|fallback|Fallback|503|relay|failover",
            ln,
        )
    ]
    # The relay does not log its drain refusals, so the turn's own log is the
    # evidence that it met the drain: the refusal body token, never a bare 503.
    met = [ln for ln in agent_lines if DRAIN_TOKEN in ln]
    gap_recovered = [
        ln for ln in agent_lines if re.search(r"local relay \S+ back after", ln)
    ]

    report = {
        "mode": a.mode,
        "tree": str(tree),
        "tree_has_relay_draining": fixed,
        "seat": a.seat,
        "turn_rc": rc,
        "wall_s": round(wall, 1),
        "stdout_tail": out_txt.strip()[-300:],
        "completed_ok": rc == 0 and "DRAIN-PROBE-OK" in out_txt,
        "relay_launches": relay.launches,
        "relay_events": len(events),
        "relay_serve_start": serve_start,
        "seats_served": seats_served,
        "models_picked": models_picked,
        "first_pick_ts": picks[0]["_ts"] if picks else None,
        "drain_window": drain_window,
        "usage": {k: usage.get(k) for k in ("model", "provider", "api_calls")}
        if isinstance(usage, dict)
        else None,
        "agent_log_drain_hits": len(met),
        "restarted": restarted,
        "listener_gap_recoveries": len(gap_recovered),
        "route_changes": route_rows,
        "failover_rows": len(failover_rows),
        "blackbox_api_calls": [list(r) for r in ledger_calls],
        "fallback_events": [list(r) for r in ledger],
        "announces": announces,
        "agent_log_excerpt": excerpt[-60:],
    }
    (work / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {k: v for k, v in report.items() if k != "agent_log_excerpt"}, indent=2
        )
    )
    print("--- agent.log excerpt ---")
    print("\n".join(excerpt[-60:]))

    ledger_real = [r for r in ledger if not str(r[0]).startswith("<")]
    ledger_live = bool(ledger_calls) and not any(
        str(r[0]).startswith("<") for r in ledger
    )
    ledger_clean = ledger_live and not ledger_real
    if not met:
        log("INCONCLUSIVE: the turn never met the drain")
        return 2
    if a.mode == "restart" and not (restarted and gap_recovered):
        log(
            "INCONCLUSIVE: restart mode needs the relay restart AND a listener-gap recovery"
        )
        return 2
    if a.expect_failover:
        ok = bool(failover_rows) and bool(ledger_real)
    else:
        ok = (
            report["completed_ok"]
            and not failover_rows
            and ledger_clean
            and not announces
        )
    log(
        ("PASS" if ok else "FAIL")
        + (" (negative control: failover expected)" if a.expect_failover else "")
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
