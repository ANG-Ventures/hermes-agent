"""t_3646c28e option C phase-3 drill rig: real `python -m gateway.run` (chaos fake platform) in a
scratch home, real kanban dispatcher + delegations board, scripted fake OpenAI endpoint.
Nothing here touches a live home: HOME/HERMES_HOME live under the drill root."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import psutil

from tests.e2e.core.chaos import _gateway_harness as gh
from tests.fakes.fake_llm_provider import FakeLLMServer, Text, ToolCall

_orig_hermetic_env = gh.hermetic_env


def _hermetic_env(home, hermes_home, tag):
    env = _orig_hermetic_env(home, hermes_home, tag)
    env["HERMES_DISABLE_LAZY_INSTALLS"] = "1"  # no PM bootstrap / network from the scratch gateway
    env["TIRITH_ENABLED"] = "false"
    env["PYTHONPATH"] = str(Path(__file__).parent / "inject") + os.pathsep + env.get("PYTHONPATH", "")
    env.update(EXTRA_ENV)
    return env


EXTRA_ENV: Dict[str, str] = {}


gh.hermetic_env = _hermetic_env

RESULT_RE = re.compile(r"CHILDRESULT-([A-Za-z0-9]+)")
GOAL_RE = re.compile(r"CHILDGOAL-([A-Za-z0-9]+)")
DRILL_RE = re.compile(r"\[drill:([A-Za-z0-9]+)\]")


def _text(m: dict) -> str:
    c = m.get("content")
    if isinstance(c, list):
        c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
    return c or ""


class Director:
    """Parent: '[drill:X] ...' -> delegate_task(goal=CHILDGOAL-X); child: terminal(sleep) -> CHILDRESULT-X."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.sleep: Dict[str, int] = {}
        self.goals: Dict[str, List[str]] = {}
        self.child_first: Dict[str, List[float]] = {}   # child model requests that START a child run
        self.child_any: Dict[str, List[float]] = {}
        self.wakes: Dict[str, List[float]] = {}         # parent requests carrying the child's result
        self.log: List[dict] = []

    def __call__(self, record: dict):
        body = record["body"]
        msgs = body.get("messages", [])
        alltext = "\n".join(_text(m) for m in msgs)
        last = msgs[-1] if msgs else {}
        cont = last.get("role") == "tool"
        users = [m for m in msgs if m.get("role") == "user"]
        first_user = _text(users[0]) if users else ""
        last_user = _text(users[-1]) if users else ""
        now = time.time()
        with self.lock:
            g = GOAL_RE.search(first_user)
            if g and not DRILL_RE.search(alltext):
                x = g.group(1)
                self.child_any.setdefault(x, []).append(now)
                self.log.append({"t": now, "who": "child", "x": x, "cont": cont, "auth": record.get("auth", "")[:12],
                                 "host": (record.get("headers") or {}).get("host")})
                if not cont:
                    self.child_first.setdefault(x, []).append(now)
                    return ToolCall("terminal", {"command": f"sleep {self.sleep.get(x, 60)}; echo SLEPT-{x}"})
                return Text(f"CHILDRESULT-{x} done")
            r = RESULT_RE.findall(last_user)
            if r and not cont:
                for x in set(r):
                    self.wakes.setdefault(x, []).append(now)
                self.log.append({"t": now, "who": "wake", "x": sorted(set(r))})
                return Text("PARENT-ACK " + " ".join(sorted(set(r))))
            d = DRILL_RE.search(last_user)
            if d and not cont:
                x = d.group(1)
                goals = self.goals.get(x) or [f"CHILDGOAL-{x}"]
                self.log.append({"t": now, "who": "parent", "x": x})
                if len(goals) == 1:
                    return ToolCall("delegate_task", {"goal": goals[0] + " run the sleep and report"})
                return ToolCall("delegate_task", {"tasks": [{"goal": gg + " run the sleep and report"} for gg in goals]})
            if cont:
                return Text("PARENT-DISPATCHED")
            self.log.append({"t": now, "who": "other", "last": last_user[:1500]})
            return Text("ok")


def write_extra(runtime: str, cwd: Path, extra: str = "") -> str:
    return (
        "plugins:\n"
        f"  enabled: [{gh.fake_platform.PLUGIN_DIR_NAME}]\n"
        "platforms:\n"
        f"  {gh.fake_platform.PLATFORM_NAME}:\n"
        "    enabled: true\n"
        "platform_toolsets:\n"
        f"  {gh.fake_platform.PLATFORM_NAME}: [terminal, delegation]\n"
        "terminal:\n"
        "  backend: local\n"
        f"  cwd: {cwd}\n"
        "approvals:\n"
        "  destructive_slash_confirm: false\n"
        "delegation:\n"
        f"  background_runtime: {runtime}\n"
        "  max_concurrent_children: 3\n"
        "kanban:\n"
        "  dispatch_in_gateway: true\n"
        "  dispatch_interval_seconds: 5\n"
        + extra
    )


class Rig:
    def __init__(self, root: Path, srv: FakeLLMServer, runtime: str, extra: str = "", tag: str = "") -> None:
        self.root = root
        self.srv = srv
        self.runtime = runtime
        self.cwd = root / "work"
        self.cwd.mkdir(parents=True, exist_ok=True)
        self.extra = extra
        self.tag = tag or gh.fake_platform.uuid.uuid4().hex[:10]
        self.gw: Optional[gh.GatewayProc] = None
        self.boots: List[dict] = []

    def start(self) -> gh.GatewayProc:
        orig = gh.gateway_extra_config
        gh.gateway_extra_config = lambda: write_extra(self.runtime, self.cwd, self.extra)
        try:
            gw = gh.GatewayProc(self.root / f"gw", self.srv.base_url, f"t3646{self.tag}")
        finally:
            gh.gateway_extra_config = orig
        gw.log_path = self.root / f"gateway-{len(self.boots)}.log"
        gw.start()
        self.gw = gw
        p = psutil.Process(gw.proc.pid)
        self.boots.append({"pid": gw.proc.pid, "create_time": p.create_time(), "t": time.time()})
        return gw

    @property
    def vhome(self) -> Path:
        return self.root / "gw" / "home" / ".hermes"

    def deleg_db(self) -> Optional[Path]:
        p = self.vhome / "kanban" / "boards" / "delegations" / "kanban.db"
        return p if p.exists() else None

    def default_db(self) -> Path:
        return self.vhome / "kanban.db"

    def cards(self) -> List[dict]:
        db = self.deleg_db()
        if db is None:
            return []
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            rows = [dict(r) for r in con.execute(
                "select id,title,status,assignee,body,worker_pid,current_run_id,created_at,completed_at from tasks")]
            for r in rows:
                r["events"] = [(e[0], e[1]) for e in con.execute(
                    "select kind,created_at from task_events where task_id=? order by id", (r["id"],))]
            return rows
        finally:
            con.close()

    def registry(self) -> dict:
        p = self.vhome / "state" / "async-delegations.json"
        if not p.exists():
            return {}
        return json.loads(p.read_text()).get("records", {})

    def sends(self, gw: Optional[gh.GatewayProc] = None) -> List[dict]:
        gw = gw or self.gw
        return [e.data for e in gw.events if e.ev == "send"]

    def wait(self, pred, timeout: float, what: str, step: float = 1.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            v = pred()
            if v:
                return v
            time.sleep(step)
        raise AssertionError(f"timeout waiting for {what}")

    def log_lines(self, needle: str) -> List[str]:
        out = []
        for f in sorted(self.root.glob("gateway-*.log")):
            for ln in f.read_text(errors="replace").splitlines():
                if needle in ln:
                    out.append(f"{f.name}: {ln}")
        for f in sorted((self.vhome / "logs").rglob("*.log")) if (self.vhome / "logs").exists() else []:
            for ln in f.read_text(errors="replace").splitlines():
                if needle in ln:
                    out.append(f"{f.relative_to(self.vhome)}: {ln}")
        return out


def proc_info(pid: int) -> dict:
    try:
        p = psutil.Process(pid)
        return {"pid": pid, "alive": p.is_running() and p.status() != psutil.STATUS_ZOMBIE,
                "create_time": p.create_time(), "ppid": p.ppid(), "cmd": " ".join(p.cmdline())[:200],
                "uid": p.uids().real}
    except psutil.Error as e:
        return {"pid": pid, "alive": False, "err": type(e).__name__}


def find_procs(needle: str, tag: str) -> List[dict]:
    out = []
    for p in psutil.process_iter(["pid", "cmdline", "environ"]):
        try:
            env = p.info.get("environ") or {}
            if env.get("CHAOS_TAG") != f"t3646{tag}":
                continue
            cmd = " ".join(p.info.get("cmdline") or [])
            if needle in cmd:
                out.append({"pid": p.pid, "cmd": cmd[:160], "create_time": p.create_time(), "ppid": p.ppid(),
                            "lineage": env.get("HERMES_KANBAN_LINEAGE")})
        except psutil.Error:
            continue
    return out
