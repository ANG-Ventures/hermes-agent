#!/usr/bin/env python3
"""Boomerang v2 SMOKE harness: AC-7 / AC-8 / AC-9 / AC-11 as one runnable command.

Dev probe, not a tool: no registry.register, and tools/dev_probes/ has no __init__.py, so
tool discovery never imports this file. Stdlib only (PyYAML when it is importable, for step 2).

Coded against the CONTRACT in plans/2026-10-18_boomerang-fork-autowake-spec.md (sections 5.4,
5.5, 5.7, 10), never against the P1-P4 branches. Every external surface sits behind an env
override so the test path runs on fakes:

  BV2_KANBAN_CMD     kanban CLI argv prefix              (default: "hermes kanban")
  BV2_SCRATCH_BOARD  scratch board slug, ^bv2-smoke       (default: bv2-smoke)
  BV2_HOME_SESSION   home session stamped on the cards    (live: required)
  BV2_ORIGIN_CHAT    Discord channel id (REST, read-only) or a capture file (.jsonl/.txt)
  BV2_GATEWAY_LOG    gateway log                          (default: <root>/logs/gateway.log)
  BV2_GATEWAY_ENV    .env holding DISCORD_BOT_TOKEN       (default: <root>/.env)
  BV2_CONFIG         config.yaml, or a dir of stage<N>.yaml snapshots (fake)
  BV2_WIRELOG_DIR    relay wire capture root              (default: ~/.claude-wirelog)
  BV2_ACK_FILE       ack file Apollo appends to           (default: <scratch>/bv2-smoke.ack)
  BV2_ACK_CMD        scripted acker argv prefix: run as `<cmd> <token>`, does the action, exit 0
                     (isolated scratch gateway lane; see puppet-lane bv2-smoke-lane.sh)
  BV2_SYNC_TIMEOUT   seconds allowed for plan.commands.wirelog_sync (default 600)
  BV2_EVIDENCE       evidence markdown                    (default: docs/boomerang-v2/evidence/smoke.md)
  BV2_PLAN           command/expectation plan             (default: fixtures/plan.json)

--dry-run executes NO kanban command (they are printed and recorded) and reads every source
once, whole. --live runs the commands against the scratch board, reads the gateway log from
its current end and polls until each expectation is met or its window closes.

The harness never edits config, never restarts anything and never writes to a board whose
slug does not start with "bv2-smoke". Steps that need Apollo print the exact action and wait
for a line in the ack file (see README.md).

Exit: 0 every check PASS; 1 any FAIL; 3 no FAIL but something INCOMPLETE/SKIP; 2 usage.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
FIXTURES = HERE / "fixtures"
DEFAULT_EVIDENCE = REPO_ROOT / "docs" / "boomerang-v2" / "evidence" / "smoke.md"

PASS, FAIL, SKIP, INCOMPLETE = "PASS", "FAIL", "SKIP", "INCOMPLETE"
BOARD_RE = re.compile(r"^bv2-smoke(?:-[a-z0-9][a-z0-9-]*)?$")

# --- contract strings (spec 5.5 / 5.7 / 7, INV-6, INV-2, task body) ---------------------------
# The spec writes an em dash and a warning emoji; the card body writes "-" and "warning". Both
# spellings are accepted so a harmless rendering choice in P3 cannot fail the smoke.
_DASH = r"\s*[-\u2013\u2014]\s*"
_WARN = r"(?:\u26a0\ufe0f?|warning)\s*"


def re_auto_handled_line(card: str) -> re.Pattern:
    return re.compile(r"\[auto-handled\]\s*" + re.escape(card) + _DASH + r"(?P<outcome>\S.*)")


def re_failed_line(card: str) -> re.Pattern:
    return re.compile(_WARN + r"auto-handle failed\s+(?:for\s+)?" + re.escape(card) + r":\s*(?P<reason>\S.*)")


def re_lost_line(card: str) -> re.Pattern:
    return re.compile(_WARN + r"auto-handle lost\s+(?:for\s+)?" + re.escape(card) + r"\b(?P<rest>.*)")


def re_notifier_line(card: str) -> re.Pattern:
    return re.compile(r"kanban notifier: auto-handled " + re.escape(card) + r" disposition=(?P<disp>[a-z_]+)")


RE_FORK_INELIGIBLE = re.compile(r"boomerang\.fork_ineligible reason=(?P<reason>[A-Za-z0-9_]+)")
RE_FORK_SERVED = re.compile(r"boomerang\.fork served_by=(?P<seat>\S+)")
RE_LOG_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:,(\d{3}))?")
HERMES_HEADER_PREFIX = "x-hermes-"


# --- small utilities ---------------------------------------------------------------------------

def hermes_root() -> Path:
    """The root home (where the gateway log/.env live), also when run under a profile home."""
    env = os.environ.get("HERMES_HOME", "").strip()
    if env:
        p = Path(env).expanduser()
        return p.parent.parent if p.parent.name == "profiles" else p
    return Path.home() / ".hermes"


def now() -> float:
    return time.time()


def iso(ts: float | None) -> str:
    if ts is None:
        return "-"
    return _dt.datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def log_line_ts(line: str) -> float | None:
    m = RE_LOG_TS.match(line)
    if not m:
        return None
    t = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
    return t + (int(m.group(2)) / 1000.0 if m.group(2) else 0.0)


def load_plan() -> dict:
    path = Path(os.environ.get("BV2_PLAN") or FIXTURES / "plan.json")
    with open(path, encoding="utf-8") as fh:
        plan = json.load(fh)
    plan["_path"] = str(path)
    return plan


def fill(template: Iterable[str], **vals: str) -> list[str]:
    return [str(part).format(**vals) for part in template]


# --- result model + evidence -------------------------------------------------------------------

@dataclass
class Check:
    name: str
    result: str
    detail: str = ""


@dataclass
class StepReport:
    step: int
    title: str
    mode: str
    started: float = field(default_factory=now)
    commands: list[str] = field(default_factory=list)
    raw: list[str] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def check(self, name: str, result: str, detail: str = "") -> None:
        self.checks.append(Check(name, result, detail))
        print(f"  [{result}] {name}" + (f" -- {detail}" if detail else ""), flush=True)

    @property
    def verdict(self) -> str:
        results = {c.result for c in self.checks}
        if not results:
            return INCOMPLETE
        if FAIL in results:
            return FAIL
        if results == {PASS}:
            return PASS
        return INCOMPLETE


def _md_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render_section(rep: StepReport) -> str:
    out = [
        f"<!-- bv2-smoke:step{rep.step}:begin -->",
        f"## Step {rep.step}: {rep.title}",
        "",
        f"- mode: `{rep.mode}`  started: {iso(rep.started)}  finished: {iso(now())}",
        f"- verdict: **{rep.verdict}**",
    ]
    out += [f"- {n}" for n in rep.notes]
    out += ["", "### Commands", "", "```text"]
    out += rep.commands or ["(none)"]
    out += ["```", "", "### Raw output", "", "```text"]
    out += rep.raw or ["(none)"]
    out += ["```", "", "### Checks", "", "| # | check | result | detail |", "|---|---|---|---|"]
    for i, c in enumerate(rep.checks, 1):
        out.append(f"| {i} | {_md_cell(c.name)} | {c.result} | {_md_cell(c.detail)} |")
    out += ["", f"<!-- bv2-smoke:step{rep.step}:end -->", ""]
    return "\n".join(out)


def write_evidence(rep: StepReport) -> Path:
    path = Path(os.environ.get("BV2_EVIDENCE") or DEFAULT_EVIDENCE)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.exists() else (
        "# Boomerang v2 smoke evidence\n\n"
        "Written by `tools/dev_probes/boomerang_v2_smoke/run.py`; one section per step, "
        "replaced on every run of that step.\n\n")
    section = render_section(rep)
    begin, end = f"<!-- bv2-smoke:step{rep.step}:begin -->", f"<!-- bv2-smoke:step{rep.step}:end -->"
    if begin in text and end in text:
        head, rest = text.split(begin, 1)
        _, tail = rest.split(end, 1)
        text = head + section.rstrip("\n") + tail
    else:
        text = text.rstrip("\n") + "\n\n" + section
    path.write_text(text, encoding="utf-8")
    return path


# --- sources -----------------------------------------------------------------------------------

class GatewayLog:
    """Gateway log lines. Live: only lines written after construction. Dry-run: the whole file.

    Live, the gateway's ``agent.log`` sibling is read too: hermes_logging routes only
    ``gateway.*`` loggers into gateway.log, while the fork lines (``boomerang.fork …``,
    ``boomerang.fork_ineligible …``) come from ``tools.delegate_tool`` and land in agent.log."""

    def __init__(self, path: Path, live: bool):
        self.path = path
        self._parts = [_LogFile(path, live)]
        sib = path.with_name("agent.log")
        if live and sib != path:
            self._parts.append(_LogFile(sib, live))

    def lines(self) -> list[str]:
        out: list[str] = []
        for part in self._parts:
            out.extend(part.lines())
        return out


class _LogFile:
    def __init__(self, path: Path, live: bool):
        self.path = path
        self.offset = path.stat().st_size if (live and path.exists()) else 0

    def lines(self) -> list[str]:
        if not self.path.exists():
            return []
        if self.path.stat().st_size < self.offset:  # rotated/truncated (e.g. across a restart)
            self.offset = 0
        with open(self.path, "rb") as fh:
            fh.seek(self.offset)
            data = fh.read()
        return data.decode("utf-8", "replace").splitlines()


@dataclass
class ChatMessage:
    ts: float | None
    content: str


class FileChat:
    """Origin-chat capture file (fake / offline): JSONL {ts, content} or plain text lines."""

    def __init__(self, path: Path):
        self.path = path
        self.label = f"file:{path}"

    def messages(self) -> list[ChatMessage]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                doc = json.loads(line)
            except ValueError:
                out.append(ChatMessage(None, line))
                continue
            if isinstance(doc, dict):
                out.append(ChatMessage(doc.get("ts"), str(doc.get("content", ""))))
        return out


class DiscordChat:
    """Read-only Discord REST read of the test channel since the step started (bot token from .env)."""

    API = "https://discord.com/api/v10"

    def __init__(self, channel_id: str, env_path: Path, since: float):
        self.channel_id = channel_id
        self.env_path = env_path
        self.label = f"discord:{channel_id}"
        self.after = str(max(0, (int(since * 1000) - 1420070400000)) << 22)

    def _token(self) -> str:
        for raw in self.env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line.startswith("export "):
                line = line[7:].lstrip()
            if line.startswith("DISCORD_BOT_TOKEN="):
                return line.split("=", 1)[1].strip().strip("'\"")
        raise SystemExit(f"DISCORD_BOT_TOKEN not found in {self.env_path}")

    def messages(self) -> list[ChatMessage]:
        url = f"{self.API}/channels/{self.channel_id}/messages?" + urllib.parse.urlencode(
            {"after": self.after, "limit": 100})
        req = urllib.request.Request(url, method="GET", headers={
            "Authorization": f"Bot {self._token()}",
            "User-Agent": "bv2-smoke (read-only, +hermes)"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            docs = json.loads(resp.read().decode("utf-8"))
        out = []
        for d in docs:
            ts = _dt.datetime.fromisoformat(d["timestamp"]).timestamp() if d.get("timestamp") else None
            text = d.get("content") or ""
            for emb in d.get("embeds") or []:
                text += "\n" + (emb.get("title") or "") + "\n" + (emb.get("description") or "")
            out.append(ChatMessage(ts, text))
        return sorted(out, key=lambda m: m.ts or 0)


def make_chat(live: bool, since: float):
    spec = os.environ.get("BV2_ORIGIN_CHAT", "").strip()
    if not spec:
        if live:
            raise SystemExit("--live needs BV2_ORIGIN_CHAT (Discord test-channel id or a capture file)")
        spec = str(FIXTURES / "fake_origin_chat.jsonl")
    if spec.isdigit():
        env_path = Path(os.environ.get("BV2_GATEWAY_ENV") or hermes_root() / ".env")
        return DiscordChat(spec, env_path, since)
    return FileChat(Path(spec).expanduser())


def gateway_log_path(live: bool) -> Path:
    spec = os.environ.get("BV2_GATEWAY_LOG", "").strip()
    if spec:
        return Path(spec).expanduser()
    return hermes_root() / "logs" / "gateway.log" if live else FIXTURES / "fake_gateway.log"


# --- the run context ---------------------------------------------------------------------------

class Ctx:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.live = bool(args.live)
        self.mode = "live" if self.live else "dry-run"
        self.plan = load_plan()
        self.board = os.environ.get("BV2_SCRATCH_BOARD", "").strip() or self.plan.get("board", "bv2-smoke")
        if not BOARD_RE.match(self.board):
            raise SystemExit(f"refusing board {self.board!r}: the scratch board slug must match {BOARD_RE.pattern}")
        self.session = os.environ.get("BV2_HOME_SESSION", "").strip()
        if self.live and not self.session:
            raise SystemExit("--live needs BV2_HOME_SESSION (the test channel's gateway session id)")
        self.session = self.session or "dry-run-session"
        self.kanban = shlex.split(os.environ.get("BV2_KANBAN_CMD") or "hermes kanban")
        self.chat_id = os.environ.get("BV2_ORIGIN_CHAT", "").strip()
        self.ack = Path(os.environ.get("BV2_ACK_FILE") or (
            Path(os.environ.get("TMPDIR") or "/tmp") / "bv2-smoke.ack" if self.live
            else FIXTURES / "fake_ack.txt"))
        self.tick = float(args.tick_seconds)
        self.slack = float(args.slack_seconds)
        self.first_tick = float(getattr(args, "first_tick_seconds", 90.0))
        self.timeout = float(args.timeout)
        self._dry_cards = dict(self.plan.get("dry_run_cards", {}))
        self.board_ready = False

    # one tick of the notifier, plus the configured slack (dispatch latency, Discord REST)
    @property
    def tick_window(self) -> float:
        return 2 * self.tick + self.slack

    def window_label(self) -> str:
        return f"waited {self.tick_window:.0f}s" if self.live else "dry-run: fixture read once"

    def kanban_cmd(self, rep: StepReport, key: str, **vals: str) -> tuple[int, str]:
        template = self.plan["commands"].get(key)
        if template is None:
            raise SystemExit(f"plan {self.plan['_path']} has no command {key!r}")
        argv = self.kanban + fill(template, board=self.board, session=self.session, chat=self.chat_id, **vals)
        shown = "$ " + shlex.join(argv)
        if not self.live:
            rep.commands.append(shown + "   # dry-run: not executed")
            return 0, ""
        print(shown, flush=True)
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=120)
        out = (proc.stdout or "") + (proc.stderr or "")
        rep.commands.append(shown + f"   # exit {proc.returncode}")
        rep.raw.append(f"--- {key} (exit {proc.returncode})\n{out.strip()[:4000]}")
        return proc.returncode, proc.stdout or ""

    def ensure_board(self, rep: StepReport) -> None:
        if self.board_ready:
            return
        rc, _ = self.kanban_cmd(rep, "board_create")
        if rc != 0:  # already exists is fine; a real failure surfaces at card create
            rep.notes.append(f"board_create exit {rc} (an existing board is fine)")
        self.board_ready = True

    def make_card(self, rep: StepReport, role: str, title: str, auto_handle: bool = True) -> str:
        """Create one scratch card (unassigned, so the dispatcher never spawns it), subscribe the
        origin chat with a wake delivery mode, and set auto_handle. Returns the card id."""
        self.ensure_board(rep)
        rc, out = self.kanban_cmd(rep, "create", title=title)
        if self.live:
            card = _parse_card_id(out)
            if rc != 0 or not card:
                raise StepAbort(f"card create failed for {role} (exit {rc})")
        else:
            card = self._dry_cards.get(role) or f"t_dry{role}"
        self.kanban_cmd(rep, "notify_subscribe", card=card)
        if auto_handle:
            rc, _ = self.kanban_cmd(rep, "set_auto_handle", card=card)
            if self.live and rc != 0:
                raise StepAbort(f"set_auto_handle failed for {card} (exit {rc}); check the flag "
                                f"name in {self.plan['_path']} against P4's kanban CLI")
        rep.notes.append(f"card `{role}` = `{card}`")
        return card

    def force(self, rep: StepReport, key: str, **vals: str) -> None:
        """Run a FORCING kanban transition; abort the step if it fails (Prism 7d1bf9b6fa33).

        A negative check ("no auto-handle within one tick") trivially passes when the event it
        waits on was never generated, so a failed transition is an abort, never a PASS.
        """
        rc, _ = self.kanban_cmd(rep, key, **vals)
        if self.live and rc != 0:
            raise StepAbort(f"forcing transition `{key}` failed (exit {rc}) for {vals}; "
                            f"no event was generated, so this step cannot assert anything")

    def poll(self, fn: Callable[[], object], window: float) -> object:
        """Call fn until it returns truthy or the window closes; dry-run reads once."""
        if not self.live:
            return fn()
        deadline = now() + window
        while True:
            got = fn()
            if got or now() >= deadline:
                return got
            time.sleep(min(1.0, max(0.05, self.tick / 2)))

    def wait_ack(self, rep: StepReport, token: str, instruction: str) -> float | None:
        """Print the action for Apollo; wait for `token` as a whole line in the ack file.

        ``BV2_ACK_CMD`` (an argv prefix) names a scripted operator: it is run as
        ``<cmd> <token>`` and must perform the action itself (a config flip, a restart of the
        gateway UNDER TEST) and exit 0. On exit 0 the token is appended to the ack file, so the
        evidence reads the same as a human ack; any other exit is a FAIL (never a silent pass).
        This is how an isolated scratch gateway (puppet-lane) drives steps 2 and 4 with no human.
        """
        banner = (f"\n>>> APOLLO ACTION ({rep.title}):\n    {instruction}\n"
                  f">>> then: echo {shlex.quote(token)} >> {self.ack}\n")
        print(banner, flush=True)
        rep.raw.append(f"--- ack request\n{instruction}\nack: echo {token} >> {self.ack}")
        ack_cmd = os.environ.get("BV2_ACK_CMD", "").strip()
        if ack_cmd and self.live:
            argv = shlex.split(ack_cmd) + [token]
            shown = "$ " + shlex.join(argv)
            print(shown, flush=True)
            try:
                proc = subprocess.run(argv, capture_output=True, text=True, timeout=self.args.ack_timeout)
                rc, out = proc.returncode, (proc.stdout or "") + (proc.stderr or "")
            except (OSError, subprocess.TimeoutExpired) as exc:
                rc, out = 127, f"{type(exc).__name__}: {exc}"
            rep.commands.append(shown + f"   # scripted ack, exit {rc}")
            rep.raw.append(f"--- ack cmd {token} (exit {rc})\n{out.strip()[:4000]}")
            if rc == 0:
                self.ack.parent.mkdir(parents=True, exist_ok=True)
                with open(self.ack, "a", encoding="utf-8") as fh:
                    fh.write(token + "\n")
            else:
                rep.check(f"ack `{token}` received", FAIL, f"BV2_ACK_CMD exit {rc}: {out.strip()[-300:]}")
                return None

        def seen() -> float | None:
            if not self.ack.exists():
                return None
            if token in {ln.strip() for ln in self.ack.read_text(encoding="utf-8").splitlines()}:
                return now()
            return None

        got = self.poll(seen, self.args.ack_timeout)
        rep.check(f"ack `{token}` received", PASS if got else FAIL,
                  f"{self.ack}" if got else f"no `{token}` line in {self.ack} within {self.args.ack_timeout:.0f}s")
        return got  # type: ignore[return-value]


class StepAbort(Exception):
    pass


def _parse_card_id(stdout: str) -> str | None:
    text = stdout.strip()
    start = text.find("{")
    if start >= 0:
        try:
            doc, _ = json.JSONDecoder().raw_decode(text[start:])
            if isinstance(doc, dict):
                return doc.get("id") or doc.get("task_id")
        except ValueError:
            pass
    m = re.search(r"\b(t_[0-9a-f]{6,})\b", text)
    return m.group(1) if m else None


# --- search helpers ----------------------------------------------------------------------------

def find_chat(chat, pattern: re.Pattern) -> ChatMessage | None:
    for msg in chat.messages():
        for line in msg.content.splitlines() or [msg.content]:
            if pattern.search(line):
                return ChatMessage(msg.ts, line.strip())
    return None


def find_log(log: GatewayLog, pattern: re.Pattern, after_ts: float | None = None,
             pred: Callable[[re.Match], bool] | None = None) -> str | None:
    for line in log.lines():
        m = pattern.search(line)
        if not m or (pred is not None and not pred(m)):
            continue
        ts = log_line_ts(line)
        if after_ts is not None and ts is not None and ts < after_ts:
            continue
        return line.strip()
    return None


# --- step 1: AC-7 / INV-6 ------------------------------------------------------------------------

def step1(ctx: Ctx) -> StepReport:
    rep = StepReport(1, "auto-handle origin-chat lines (AC-7, INV-5, INV-6)", ctx.mode)
    t0 = now()
    log = GatewayLog(gateway_log_path(ctx.live), ctx.live)
    chat = make_chat(ctx.live, t0)
    rep.notes.append(f"board `{ctx.board}`; gateway log `{log.path}`; origin chat `{chat.label}`")

    done_card = ctx.make_card(rep, "s1_completed", "bv2 smoke s1: completed -> auto-handled")
    ni_card = ctx.make_card(rep, "s1_needs_input", "bv2 smoke s1: needs_input -> surface")
    # Dry-run evaluates the failure-line assertion against the fake chat even with no fault hook
    # configured (it exercises the matcher); live without a hook reports SKIP, never a fake PASS.
    inject = ctx.plan["commands"].get("inject_failure")
    arm_failure = bool(inject) or not ctx.live
    fail_card = (ctx.make_card(rep, "s1_failure", "bv2 smoke s1: forced child failure -> warning")
                 if arm_failure else "<no fault hook>")
    if inject and "s1_fault_hold" in ctx.plan["commands"]:
        ctx.force(rep, "s1_fault_hold", card=fail_card)  # keeps the child busy past the fault

    ctx.force(rep, "complete", card=done_card)
    ctx.force(rep, "block_needs_input", card=ni_card)
    win = ctx.timeout
    fail_dispatch = None
    if inject:
        # AC-7 live fault (t_480d08ce): the home stays live and the DISPATCHED child's verdict is
        # refused. A board write by another session after the claim (inject_failure = a kanban
        # comment) makes the executor's freshness check (kanban_auto_handle._moved) return
        # "state moved", so run_entry -> _fail posts the warning AND re-surfaces the wake into the
        # live home. Rehoming the card (the old fault) only proved the warning half.
        ctx.force(rep, "complete", card=fail_card)
        fail_dispatch = ctx.poll(lambda: _auto_disp(find_log(log, re_notifier_line(fail_card)), fail_card), win)
        rep.check(f"failure card {fail_card}: auto-handle child dispatched before the fault",
                  PASS if fail_dispatch else FAIL, fail_dispatch or f"absent after {win:.0f}s")
        if fail_dispatch:
            ctx.force(rep, "inject_failure", card=fail_card)

    hit = ctx.poll(lambda: find_chat(chat, re_auto_handled_line(done_card)), win)
    rep.check(f"origin chat: `[auto-handled] {done_card} - <Outcome>`", PASS if hit else FAIL,
              hit.content if hit else f"absent after {win:.0f}s")
    if hit:
        rep.raw.append(f"--- chat match\n{hit.content}")
    line = ctx.poll(lambda: find_log(log, re_notifier_line(done_card)), ctx.tick_window)
    disp = re_notifier_line(done_card).search(line).group("disp") if line else None
    rep.check(f"gateway log: `kanban notifier: auto-handled {done_card} disposition=auto`",
              PASS if disp == "auto" else FAIL, line or "absent")
    if line:
        rep.raw.append(f"--- log match\n{line}")

    # needs_input is a judgment event: it must surface, never auto-handle (INV-5).
    def ni_auto():
        return find_chat(chat, re_auto_handled_line(ni_card))
    neg = ctx.poll(ni_auto, ctx.tick_window)  # live: waits the whole window for a line that must not come
    rep.check(f"needs_input card {ni_card}: no `[auto-handled]` line", FAIL if neg else PASS,
              neg.content if neg else f"none ({ctx.window_label()})")
    ni_log = find_log(log, re_notifier_line(ni_card))
    ni_disp = re_notifier_line(ni_card).search(ni_log).group("disp") if ni_log else None
    rep.check(f"needs_input card {ni_card}: notifier disposition is not auto",
              FAIL if ni_disp == "auto" else PASS, ni_log or "no auto-handled log line (surfaced as today)")

    if not arm_failure:
        rep.check(f"failure card {fail_card}: `warning auto-handle failed {fail_card}: <reason>`", SKIP,
                  "plan.commands.inject_failure is null: no fault-injection hook configured (README)")
    else:
        warn = ctx.poll(lambda: find_chat(chat, re_failed_line(fail_card)), win)
        rep.check(f"origin chat: `warning auto-handle failed {fail_card}: <reason>`", PASS if warn else FAIL,
                  warn.content if warn else f"absent after {win:.0f}s")
        if warn:
            rep.raw.append(f"--- chat match\n{warn.content}")
        if inject:
            _check_live_resurface(ctx, rep, log, chat, fail_card, warn, fail_dispatch)
    return rep


def _check_live_resurface(ctx: Ctx, rep: StepReport, log: GatewayLog, chat, card: str,
                          warn: "ChatMessage | None", dispatched: str | None) -> None:
    """INV-6 second half, live: the injected fault left the home live, so the original wake
    must come back into the home session as a turn (not `no live home ...`)."""
    expect = ctx.plan.get("expect", {})
    reason_re = re.compile(expect.get("fault_reason_regex", r"state moved"))
    reason = re_failed_line(card).search(warn.content).group("reason") if warn else None
    rep.check(f"failure card {card}: warning reason is the injected fault (/{reason_re.pattern}/)",
              PASS if reason and reason_re.search(reason) else FAIL, reason or "no warning line")
    early = find_chat(chat, re_auto_handled_line(card))
    if early:
        rep.check(f"failure card {card}: fault landed before the verdict", INCOMPLETE,
                  f"`{early.content}` posted: the child finished before inject_failure; re-run "
                  f"(or set commands.s1_fault_hold)")
        return
    after = log_line_ts(dispatched) if (ctx.live and dispatched) else None
    re_wake = re.compile(expect.get("resurface_wake_log_regex",
                                    r"inbound message: .*msg=.\[kanban\] Task {card}\b").format(card=re.escape(card)))
    # a positive expectation: a busy home queues the turn until its current turn ends (AC-11 row 2166658)
    wake = ctx.poll(lambda: find_log(log, re_wake, after_ts=after), ctx.timeout)
    rep.check(f"home session: re-delivered wake turn for {card}", PASS if wake else FAIL,
              wake or f"no line matching /{re_wake.pattern}/")
    if wake:
        rep.raw.append(f"--- log match\n{wake}")
    re_dead = re.compile(r"no live home for " + re.escape(card) + r"\b")
    dead = find_log(log, re_dead, after_ts=after)
    rep.check(f"home stayed live: no `no live home for {card}` line", FAIL if dead else PASS,
              dead or "none")


# --- step 2: AC-8 / INV-7 ------------------------------------------------------------------------

def _read_config(ctx: Ctx, stage: int) -> dict:
    spec = os.environ.get("BV2_CONFIG", "").strip()
    if spec:
        path = Path(spec).expanduser()
    else:
        path = hermes_root() / "config.yaml" if ctx.live else FIXTURES / "fake_config"
    if path.is_dir():  # fake: one snapshot per stage
        path = path / f"stage{stage}.yaml"
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # noqa: PLC0415 - optional, the venv carries it
        return yaml.safe_load(text) or {}
    except ImportError:
        return _mini_yaml(text)


def _mini_yaml(text: str) -> dict:
    """Indentation-nested `key: scalar` reader; enough for the two booleans step 2 reads."""
    root: dict = {}
    stack: list[tuple[int, dict]] = [(-1, root)]
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#") or ":" not in raw:
            continue
        indent = len(raw) - len(raw.lstrip())
        key, _, val = raw.strip().partition(":")
        while stack[-1][0] >= indent:
            stack.pop()
        val = val.split(" #", 1)[0].strip()
        if not val:
            node: dict = {}
            stack[-1][1][key] = node
            stack.append((indent, node))
        else:
            low = val.lower()
            stack[-1][1][key] = True if low == "true" else False if low == "false" else val
    return root


def _get(cfg: dict, dotted: str):
    cur: object = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _effective(raw, default: bool) -> bool:
    return default if raw is None else bool(raw)


SWITCH_AUTO = "delegation.boomerang.auto_handle_enabled"
SWITCH_FORK = "delegation.boomerang.fork_enabled"
# INV-7: auto_handle_enabled defaults false; fork_enabled is the opt-out (unset = forks allowed).
SWITCH_DEFAULTS = {SWITCH_AUTO: False, SWITCH_FORK: True}


def step2(ctx: Ctx) -> StepReport:
    rep = StepReport(2, "kill switches take effect on the next tick (AC-8, INV-7)", ctx.mode)
    log = GatewayLog(gateway_log_path(ctx.live), ctx.live)
    chat = make_chat(ctx.live, rep.started)
    rep.notes.append("the harness never edits config: it prints each one-line diff for Apollo and "
                     "waits on the ack file")

    stage = 0
    cfg = _read_config(ctx, stage)
    pre = {k: _effective(_get(cfg, k), d) for k, d in SWITCH_DEFAULTS.items()}
    rep.raw.append(f"--- config stage {stage}\n{SWITCH_AUTO}={pre[SWITCH_AUTO]} {SWITCH_FORK}={pre[SWITCH_FORK]}")
    ok_pre = pre[SWITCH_AUTO] and pre[SWITCH_FORK]
    rep.check("precondition: auto_handle_enabled=true and fork_enabled=true", PASS if ok_pre else FAIL,
              f"auto_handle_enabled={pre[SWITCH_AUTO]} fork_enabled={pre[SWITCH_FORK]}")
    if not ok_pre:
        return rep

    def flip(key: str, value: bool, token: str) -> bool:
        nonlocal stage
        diff = f"config.yaml: {key}: {str(not value).lower()} -> {str(value).lower()}"
        rep.commands.append(f"# APOLLO (manual): {diff}   ack: {token}")
        if not ctx.wait_ack(rep, token, diff):
            return False
        stage += 1
        got = _effective(_get(_read_config(ctx, stage), key), SWITCH_DEFAULTS[key])
        rep.check(f"config reads {key}={str(value).lower()} after ack", PASS if got == value else FAIL,
                  f"read {str(got).lower()} (stage {stage})")
        return got == value

    # (a) fork_enabled=false with auto-handle on: the next fork request must downgrade (INV-7 P2 kill).
    if flip(SWITCH_FORK, False, "fork_enabled=false"):
        t = now()
        card = ctx.make_card(rep, "s2_fork_off", "bv2 smoke s2: fork_enabled=false -> summary")
        ctx.force(rep, "complete", card=card)
        line = ctx.poll(lambda: find_log(log, RE_FORK_INELIGIBLE, after_ts=t if ctx.live else None,
                                         pred=lambda m: m.group("reason") == "fork_enabled"), ctx.timeout)
        rep.check("gateway log: `boomerang.fork_ineligible reason=fork_enabled` after the flip",
                  PASS if line else FAIL, line or f"absent after {ctx.timeout:.0f}s")
        if line:
            rep.raw.append(f"--- log match\n{line}")
            ts = log_line_ts(line)
            if ctx.live and ts is not None:
                rep.notes.append(f"fork_enabled: event forced {iso(t)}, downgrade logged {iso(ts)} "
                                 f"({ts - t:.1f}s; tick {ctx.tick:.0f}s)")
        hit = ctx.poll(lambda: find_chat(chat, re_auto_handled_line(card)), ctx.timeout)
        rep.check(f"origin chat: `[auto-handled] {card}` still posts (summary-mode child)",
                  PASS if hit else FAIL, hit.content if hit else "absent")

    # (b) auto_handle_enabled=false: the next wake on an auto_handle card is NOT auto-handled.
    if flip(SWITCH_AUTO, False, "auto_handle_enabled=false"):
        card = ctx.make_card(rep, "s2_auto_off", "bv2 smoke s2: auto_handle_enabled=false -> surface")
        ctx.force(rep, "complete", card=card)
        neg = ctx.poll(lambda: find_chat(chat, re_auto_handled_line(card)) or
                       _auto_disp(find_log(log, re_notifier_line(card)), card), ctx.tick_window)
        detail = getattr(neg, "content", neg) if neg else f"no auto-handle ({ctx.window_label()})"
        rep.check(f"card {card}: not auto-handled within one tick of the flip", FAIL if neg else PASS,
                  str(detail))

    # restore both (Apollo), verify the config reads the pre-run values again
    rep.commands.append(f"# APOLLO (manual): restore {SWITCH_FORK}: true and {SWITCH_AUTO}: true   ack: restore")
    if ctx.wait_ack(rep, "restore", f"config.yaml: restore {SWITCH_FORK}: false -> true and "
                    f"{SWITCH_AUTO}: false -> true"):
        stage += 1
        cfg = _read_config(ctx, stage)
        back = all(_effective(_get(cfg, k), d) for k, d in SWITCH_DEFAULTS.items())
        rep.check("config restored: both switches true", PASS if back else FAIL, f"stage {stage}")
    return rep


def _auto_disp(line: str | None, card: str) -> str | None:
    m = re_notifier_line(card).search(line or "")
    return line if (m and m.group("disp") == "auto") else None


# --- step 3: AC-9 / INV-8 ------------------------------------------------------------------------

def wirelog_dir(live: bool) -> Path:
    spec = os.environ.get("BV2_WIRELOG_DIR", "").strip()
    if spec:
        return Path(spec).expanduser()
    return Path.home() / ".claude-wirelog" if live else FIXTURES / "fake_wirelog"


def _iter_wire_records(root: Path) -> Iterable[dict]:
    import gzip  # noqa: PLC0415
    for path in sorted(list(root.rglob("*.jsonl")) + list(root.rglob("*.jsonl.gz"))):
        opener = gzip.open if path.suffix == ".gz" else open
        try:
            with opener(path, "rt", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(rec, dict):
                        yield rec
        except (OSError, EOFError):
            continue


def _rec_ts(rec: dict) -> float | None:
    ts = rec.get("ts")
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, str):
        try:
            return _dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


# The relay names the Studio's own seat three ways: `local`, `sub-0`, and wire `sub: 0`.
_LOCAL_SEAT_ALIASES = frozenset({"local", "sub-0", "0"})


def _norm_seat(v: object) -> str | None:
    if isinstance(v, bool) or v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    if s in _LOCAL_SEAT_ALIASES:
        return "local"
    # The relay's `x-pool-served-by` names the box (`sub-vps-16`); its wire records carry `sub: "16"`
    # (plus `host: sub-vps-16`). Both are the same seat: compare on the bare number (live 2026-10-19:
    # every step-3 run read "0 records attributable to sub-vps-16" against 22 records from that box).
    m = re.fullmatch(r"sub-(?:vps-)?(\d+)", s)
    if m:
        return "local" if m.group(1) == "0" else f"sub-{m.group(1)}"
    return f"sub-{s}" if s.isdigit() else s


def _rec_seat(rec: dict) -> str | None:
    """The serving seat a wire record belongs to (relay-wire-forensics `served_by`/`seat`/`sub`), normalized."""
    meta = rec.get("meta") if isinstance(rec.get("meta"), dict) else {}
    for v in (rec.get("served_by"), rec.get("seat"), meta.get("served_by"), rec.get("sub")):
        n = _norm_seat(v)
        if n:
            return n
    return None


def _header_names(rec: dict) -> list[str]:
    req = rec.get("req") or {}
    names: list[str] = []
    hdrs = req.get("headers")
    if isinstance(hdrs, list):
        names += [str(p[0]) for p in hdrs if isinstance(p, (list, tuple)) and p]
    elif isinstance(hdrs, dict):
        names += [str(k) for k in hdrs]
    if isinstance(req.get("headerOrder"), list):
        names += [str(n) for n in req["headerOrder"]]
    return names


# --- AC-9 positive control (Argus caveat 2, t_480d08ce) ------------------------------------------
# Which hop passes an UNKNOWN `x-hermes-*` header? None does:
#   - claude-pool relay: partition_headers forwards only _FORWARD_HEADERS (content-type,
#     anthropic-version, anthropic-beta; claude_pool_relay.py:881, :1162) and http_send adds only
#     the box-bound _CORRELATION_HEADERS x-hermes-route-id / x-hermes-lane-src (:897-899, :1322).
#   - apx box: OUTBOUND_HEADER_ALLOWLIST drops every x-hermes-* (proxy.js:755-766, :9167-9175) and
#     assertNoFleetHeaders re-strips the final wire set (proxy.js:951, :9986). The wirelog records
#     `getHeaders()` of that outbound request (wirelog.js:1239), i.e. AFTER the strip.
# So an `x-hermes-qa-canary` header can never reach a capture. The control therefore uses the one
# caller-supplied x-hermes-* header the relay DOES carry to the box and the box DOES record:
# `x-hermes-lane-src`, which apx parses into the record's top-level `lane_src` (proxy.js:906-915,
# wirelog.js:1041). A nonce in `lane_src.platform` proves a fleet header reached the capture point
# for that very request; its `req.headers` must still carry no x-hermes-* name.
CANARY_LANE_SRC_HEADER = "x-hermes-lane-src"
CANARY_HEADER = "x-hermes-qa-canary"   # sent too; expected dropped by the relay, never recorded
CANARY_PREFIX = "bv2canary"


def canary_token(live: bool, plan: dict) -> str:
    if not live:
        return str(plan.get("dry_run_canary") or f"{CANARY_PREFIX}000000000000")
    import secrets  # noqa: PLC0415

    return CANARY_PREFIX + secrets.token_hex(6)


def _rec_canary(rec: dict) -> str | None:
    lane = rec.get("lane_src")
    plat = lane.get("platform") if isinstance(lane, dict) else None
    return str(plat) if isinstance(plat, str) and plat.startswith(CANARY_PREFIX) else None


def send_canary(url: str, token: str, model: str, timeout: float = 120.0) -> tuple[int, str | None, str]:
    """One minimal Messages request through the relay. Returns (status, served_by, detail)."""
    body = json.dumps({"model": model, "max_tokens": 1,
                       "messages": [{"role": "user", "content": "Reply with: ok"}]}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "content-type": "application/json", "anthropic-version": "2023-06-01",
        CANARY_LANE_SRC_HEADER: f"platform={token};delegate_depth=0;aux_task=-",
        CANARY_HEADER: token, "user-agent": "bv2-smoke-canary"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.headers.get("x-pool-served-by"), "ok"
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("x-pool-served-by") if exc.headers else None, str(exc)
    except (OSError, ValueError) as exc:
        return 0, None, f"{type(exc).__name__}: {exc}"


def _canary_control(ctx: Ctx, rep: StepReport) -> str:
    """Send the canary (live) and sync its seat; returns the token the scan must find."""
    token = canary_token(ctx.live, ctx.plan)
    url = os.environ.get("BV2_CANARY_URL") or ctx.plan.get("canary_url") or "http://127.0.0.1:18810/v1/messages"
    model = ctx.plan.get("canary_model") or "claude-haiku-4-5"
    shown = (f"$ POST {url}  {CANARY_LANE_SRC_HEADER}: platform={token};delegate_depth=0;aux_task=-  "
             f"{CANARY_HEADER}: {token}  model={model} max_tokens=1")
    if not ctx.live:
        rep.commands.append(shown + "   # dry-run: not sent")
        return token
    status, served, detail = send_canary(url, token, model)
    rep.commands.append(shown + f"   # HTTP {status} served_by={served}")
    rep.raw.append(f"--- canary\nHTTP {status} served_by={served} {detail}")
    _sync_seat(ctx, rep, _norm_seat(served) if served else None, "canary")
    return token


def _sync_seat(ctx: Ctx, rep: StepReport, seat: str | None, what: str) -> None:
    """Pull a remote seat's wire capture locally (``plan.commands.wirelog_sync``) so the scan can
    see the request it served. Used for the canary's seat AND the fork's: the pool pins the fork
    to the parent's seat, which is only known from the ``served_by=`` line, so the lane that
    drives the harness cannot sync it beforehand (t_b901c171, 2026-10-20: a step-3 run went
    INCOMPLETE on `sub-vps-9` with the canary green on `sub-19`)."""
    sync = ctx.plan.get("commands", {}).get("wirelog_sync")
    m = re.fullmatch(r"sub-(\d+)", seat or "")
    if sync and m:
        time.sleep(ctx.slack)   # let the box flush the record before the pull
        argv = [os.path.expanduser(a) for a in fill(sync, sub=m.group(1))]
        # BV2_SYNC_TIMEOUT: a one-seat `wirelog-fleet.py sync` measured 21m50s wall on the Studio
        # at load ~80 (2026-10-20 07:1x; the retention reference walk dominates), past the old
        # hard 600 s. A timeout is recorded as a non-zero exit, never swallowed.
        budget = float(os.environ.get("BV2_SYNC_TIMEOUT") or 600)
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=budget)
            rc, out = proc.returncode, (proc.stdout or "") + (proc.stderr or "")
        except subprocess.TimeoutExpired as exc:
            rc, out = 124, f"wirelog_sync timed out after {budget:.0f}s (BV2_SYNC_TIMEOUT): {exc}"
        rep.commands.append("$ " + shlex.join(argv) + f"   # {what} seat, exit {rc}")
        rep.raw.append(f"--- wirelog_sync {what} {seat} (exit {rc})\n" + out.strip()[-2000:])
    elif seat and seat != "local":
        rep.notes.append(f"{what} served by `{seat}`: no plan.commands.wirelog_sync, so its record is only "
                         f"visible after `wirelog-fleet.py sync --sub {seat[4:]}`")


def step3(ctx: Ctx) -> StepReport:
    rep = StepReport(3, "no x-hermes-* header upstream on a fork turn (AC-9, INV-8)", ctx.mode)
    t0 = now()
    log = GatewayLog(gateway_log_path(ctx.live), ctx.live)
    root = wirelog_dir(ctx.live)
    rep.notes.append(f"wire capture root `{root}` (relay-wire-forensics apx/bpx JSONL schema)")
    rep.commands.append(f"$ python3 ~/.hermes/scripts/wirelog-coverage-check.py   # confirm the serving "
                        f"lane is capturing before trusting a clean result")
    canary = _canary_control(ctx, rep)

    if not ctx.args.no_trigger:
        card = ctx.make_card(rep, "s3_fork", "bv2 smoke s3: one fork turn for wire capture")
        ctx.force(rep, "complete", card=card)
    served = ctx.poll(lambda: find_log(log, RE_FORK_SERVED), ctx.timeout)
    seat = RE_FORK_SERVED.search(served).group("seat") if served else None
    rep.check("gateway log: `boomerang.fork served_by=<seat>` (a fork turn happened)",
              PASS if served else FAIL, served or f"absent after {ctx.timeout:.0f}s")
    if served:
        rep.raw.append(f"--- log match\n{served}")
    if seat and seat not in {"local", "sub-0", "0"}:
        rep.notes.append(f"served by `{seat}`: its capture is on that box; run `wirelog-fleet.py sync` and "
                         f"point BV2_WIRELOG_DIR at ~/.claude-wirelog-fleet before trusting step 3")
        if ctx.live:
            _sync_seat(ctx, rep, _norm_seat(seat), "fork")

    # give the box a moment to flush the record (live), then read the window
    t_end = now() + (ctx.slack if ctx.live else 0)
    if ctx.live:
        time.sleep(max(0.0, t_end - now()))
    recs = []
    canary_recs = []
    for rec in _iter_wire_records(root):
        ts = _rec_ts(rec)
        if ctx.live and (ts is None or ts < t0 - 1):
            continue
        if _rec_canary(rec) == canary:   # the control may be served by any seat
            canary_recs.append(rec)
            continue
        # Prism 7555e6381e68: only records from the box that SERVED the fork count as coverage;
        # unrelated clean traffic from another seat must not stand in for the fork request.
        if seat and _rec_seat(rec) not in (None, _norm_seat(seat)):
            continue
        recs.append(rec)
    # Positive control BEFORE the scan: without a seen canary, "0 leaks" may be blindness.
    if canary_recs:
        c = canary_recs[0]
        rep.check(f"positive control: canary `{canary}` recorded (fleet header reached the capture)", PASS,
                  f"{c.get('id', '?')} lane={c.get('lane')} sub={c.get('sub')} lane_src.platform={canary}")
    else:
        rep.check(f"positive control: canary `{canary}` recorded (fleet header reached the capture)",
                  INCOMPLETE, f"no record under {root} with lane_src.platform={canary}: the capture's "
                  f"view of x-hermes-* headers is unproven, so a clean scan is blind, not a PASS")
    if not recs:
        rep.check("wire records captured in the fork-turn window", INCOMPLETE,
                  f"0 records under {root} attributable to seat `{seat}`: no visibility is not a "
                  f"clean result (coverage gap, or the serving box's capture is not synced)")
        return rep
    if seat and not any(_rec_seat(r) == _norm_seat(seat) for r in recs):
        rep.check("wire records attributable to the serving seat", INCOMPLETE,
                  f"{len(recs)} record(s) in the window but none carries seat `{seat}`; cannot prove "
                  f"the fork request was inspected")
        return rep
    rep.check("wire records captured in the fork-turn window", PASS, f"{len(recs)} record(s), seat `{seat}`")
    leaks = []
    for rec in canary_recs + recs:   # the canary's upstream header set must be clean too
        bad = sorted({n for n in _header_names(rec) if n.lower().startswith(HERMES_HEADER_PREFIX)})
        if bad:
            leaks.append(f"{rec.get('id', '?')}: {', '.join(bad)}")
    rep.raw.append("--- upstream header scan\n" + "\n".join(
        f"{r.get('id', '?')} lane={r.get('lane')} sub={r.get('sub')} headers={len(_header_names(r))}"
        for r in recs[:50]))
    rep.check("no `x-hermes-*` header in any upstream request", FAIL if leaks else PASS,
              "; ".join(leaks) if leaks else f"0/{len(recs)} records carry one")
    return rep


# --- step 4: AC-11 / 5.7 -------------------------------------------------------------------------

def _boot_id(path: Path) -> str | None:
    try:
        return str(json.loads(path.read_text(encoding="utf-8")).get("boot_id"))
    except (OSError, ValueError, AttributeError):
        return None


def step4(ctx: Ctx) -> StepReport:
    rep = StepReport(4, "restart drill observer: reconciler re-surfaces within one tick (AC-11, 5.7)", ctx.mode)
    log = GatewayLog(gateway_log_path(ctx.live), ctx.live)
    chat = make_chat(ctx.live, rep.started)
    pid_path = Path(os.environ.get("BV2_GATEWAY_PID") or (
        hermes_root() / "gateway.pid" if ctx.live else FIXTURES / "fake_gateway.pid"))
    boot_before = _boot_id(pid_path)
    rep.notes.append(f"the harness never restarts anything; gateway pid file `{pid_path}` boot_id before: "
                     f"`{boot_before}`")
    re_restart = re.compile(ctx.plan["expect"]["restart_log_regex"])

    card = ctx.make_card(rep, "s4_restart", "bv2 smoke s4: child in flight across a gateway restart")
    if "s4_hold" in ctx.plan.get("commands", {}):
        # The drill needs a child that is STILL RUNNING when the restart lands. A forced completion is
        # accepted in ~40 s, faster than safe-restart's drain + relaunch (~70 s live, 2026-10-19 18:58),
        # so the plan can give the card a body that keeps the child busy for a few minutes.
        ctx.force(rep, "s4_hold", card=card)
    ctx.force(rep, "complete", card=card)
    dispatched = ctx.poll(lambda: _auto_disp(find_log(log, re_notifier_line(card)), card), ctx.timeout)
    rep.check(f"auto-handle child dispatched for {card} (disposition=auto)", PASS if dispatched else FAIL,
              dispatched or f"absent after {ctx.timeout:.0f}s")
    if not dispatched:
        return rep
    rep.raw.append(f"--- log match\n{dispatched}")

    instr = ("restart the gateway NOW via safe-gateway-restart (single-use approval), while the "
             f"auto-handle child for {card} is still running")
    rep.commands.append(f"# APOLLO (manual): {instr}   ack: restart")
    if not ctx.wait_ack(rep, "restart", instr):
        return rep

    early = find_chat(chat, re_auto_handled_line(card))
    dispatch_ts = log_line_ts(dispatched)  # a boot line BEFORE the dispatch is not this restart
    restart_line = ctx.poll(lambda: find_log(log, re_restart, after_ts=dispatch_ts), ctx.timeout)
    rep.check("gateway restart observed in the log", PASS if restart_line else FAIL,
              restart_line or f"no line matching /{re_restart.pattern}/")
    if not restart_line:
        return rep
    rep.raw.append(f"--- log match\n{restart_line}")
    boot_after = _boot_id(pid_path)
    if ctx.live:
        rep.check("gateway boot_id changed", PASS if boot_after and boot_after != boot_before else FAIL,
                  f"{boot_before} -> {boot_after}")
    restart_ts = log_line_ts(restart_line)
    if early and (early.ts is None or restart_ts is None or early.ts < restart_ts):
        rep.check("child was still in flight at restart", INCOMPLETE,
                  f"`[auto-handled] {card}` posted before the restart; the drill did not cut a live child, re-run")
        return rep

    re_resurface = re.compile(ctx.plan["expect"]["resurface_log_regex"].format(card=re.escape(card)))
    # §5.7: the reconciler runs at the END of the first notifier tick after boot. That tick starts
    # after the watcher wiring + the notifier's 5 s initial delay and, on a loaded host, one full tick
    # over every subscription takes tens of seconds (live 2026-10-20 00:05: line at +27.5 s, load ~70).
    # The window is "the first tick after startup", bounded by --first-tick-seconds, not one idle tick.
    first_tick = ctx.first_tick if ctx.live else ctx.tick_window + ctx.tick
    resurf = ctx.poll(lambda: find_log(log, re_resurface, after_ts=restart_ts if ctx.live else None),
                      first_tick)
    rep.check(f"reconciler re-surfaced the wake for {card}", PASS if resurf else FAIL,
              resurf or f"no line matching /{re_resurface.pattern}/")
    if resurf:
        rep.raw.append(f"--- log match\n{resurf}")
        rts = log_line_ts(resurf)
        if ctx.live and rts is not None and restart_ts is not None:
            lag = rts - restart_ts
            limit = first_tick
            rep.check("re-surface on the first notifier tick after the restart", PASS if lag <= limit else FAIL,
                      f"{lag:.1f}s after `{restart_line[:19]}` (limit {limit:.0f}s = wiring + 5 s initial delay "
                      f"+ one full notifier tick, --first-tick-seconds)")
    lost = ctx.poll(lambda: find_chat(chat, re_lost_line(card)), ctx.tick_window + ctx.tick)
    rep.check(f"origin chat: `warning auto-handle lost {card} (reason: gateway restart)`",
              PASS if lost else FAIL, lost.content if lost else "absent")
    # Prism b2a4a7e824f7: watch for a stale child's late completion for a FULL tick window after the
    # lost-warning, not a single read; a post one second after the last read must still fail the drill.
    late = ctx.poll(lambda: find_chat(chat, re_auto_handled_line(card)), ctx.tick_window)
    rep.check(f"no late `[auto-handled] {card}` within {ctx.window_label()} after the re-surface",
              FAIL if late else PASS, late.content if late else "none")
    return rep


# --- entry point -------------------------------------------------------------------------------

STEPS = {1: step1, 2: step2, 3: step3, 4: step4}
APOLLO_STEPS = {2: "config flips (3 acks)", 4: "gateway restart (1 ack)"}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run.py",
        description="Boomerang v2 smoke harness (AC-7/8/9/11). Steps needing Apollo: "
                    + "; ".join(f"{k}: {v}" for k, v in APOLLO_STEPS.items())
                    + ". Apollo acks by appending the printed token as a line to the ack file.")
    p.add_argument("--step", required=True, choices=["1", "2", "3", "4", "all"])
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="execute no kanban command; read fakes once")
    mode.add_argument("--live", action="store_true", help="run against the scratch board and live sources")
    p.add_argument("--tick-seconds", type=float, default=5.0,
                   help="kanban notifier tick (gateway/kanban_watchers.py interval, default 5)")
    p.add_argument("--first-tick-seconds", type=float, default=90.0,
                   help="step 4: bound on the FIRST notifier tick after a gateway boot (wiring + 5 s "
                        "initial delay + one full tick over every subscription); default 90")
    p.add_argument("--slack-seconds", type=float, default=10.0,
                   help="latency allowance on top of a tick (dispatch, Discord REST); default 10")
    p.add_argument("--timeout", type=float, default=600.0,
                   help="max wait for a positive expectation (a child finishing), default 600")
    p.add_argument("--ack-timeout", type=float, default=1800.0, help="max wait for an Apollo ack, default 1800")
    p.add_argument("--no-trigger", action="store_true",
                   help="step 3: do not mint a card; observe a fork turn started elsewhere")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        ctx = Ctx(args)
    except SystemExit as exc:
        print(f"run.py: {exc}", file=sys.stderr)
        return 2
    steps = [1, 2, 3, 4] if args.step == "all" else [int(args.step)]
    verdicts = []
    for n in steps:
        print(f"== step {n} ({ctx.mode}) ==", flush=True)
        try:
            rep = STEPS[n](ctx)
        except StepAbort as exc:
            rep = StepReport(n, f"step {n} aborted", ctx.mode)
            rep.check("step setup", FAIL, str(exc))
        except SystemExit as exc:
            print(f"run.py: {exc}", file=sys.stderr)
            return 2
        path = write_evidence(rep)
        print(f"== step {n}: {rep.verdict} (evidence: {path})", flush=True)
        verdicts.append(rep.verdict)
    if FAIL in verdicts:
        return 1
    return 0 if all(v == PASS for v in verdicts) else 3


if __name__ == "__main__":
    sys.exit(main())
