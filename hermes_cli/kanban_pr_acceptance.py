"""Exact-head GitHub acceptance for explicitly declared PR tasks.

Network work happens outside SQLite transactions. The lifecycle owner persists
receipts only after rechecking the captured run/status/contract under its lock.

``gh`` runs as the card's assignee profile (``profile_home``), not the ambient
login: :func:`_gh_env` resolves that profile's own credentials/config for the
subprocess — a multi-profile host's default ``gh`` login cannot read another
org's private repos (#122689).
"""
from __future__ import annotations

import os
import json
import re
import subprocess
from pathlib import Path
from urllib.parse import quote

_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_INTEGRATION_DENIED = "Resource not accessible by integration"
_PR = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)")


def validate_contract(value: str | None) -> str:
    if value is None or value == "local-only":
        return "local-only"
    if not isinstance(value, str) or not (_REPO.fullmatch(value) or _PR.fullmatch(value)):
        raise ValueError("completion_contract must be local-only, OWNER/REPO, or an exact GitHub PR URL")
    return value


def _api(endpoint: str, *, query: str | None = None, paginate: bool = False,
         profile_home: str | None = None):
    command = ["gh", "api", endpoint, "--hostname", "github.com"]
    if query is not None:
        command += ["-f", "query=" + query]
    if paginate:
        command += ["--paginate", "--slurp"]
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=30,
                                check=True, env=_gh_env(profile_home))
    except subprocess.CalledProcessError as exc:
        # 401/403/404 = the login cannot see this repository (wrong profile identity
        # or missing grant), not a transient API failure. Persist only the status
        # code + endpoint, never gh's stderr (credentials/host details).
        denied = re.search(r"HTTP (40[134])", exc.stderr or "")
        if denied:
            raise _GateAuthError(f"HTTP {denied[1]} on {endpoint.split('?')[0]}") from None
        if _INTEGRATION_DENIED in (exc.stderr or ""):
            # GraphQL answers a field-level permission refusal with HTTP 200 + errors, so
            # gh exits 1 without an "HTTP 403" marker: still a login/permission problem.
            raise _GateAuthError(f"integration permission refused on {endpoint.split('?')[0]}") from None
        if exc.returncode == 4:  # gh's authentication-required exit: this profile has no login
            raise _GateAuthError(f"gh has no login for {endpoint.split('?')[0]}") from None
        raise
    value = json.loads(result.stdout)
    if isinstance(value, dict) and value.get("errors"):
        if any((e or {}).get("type") == "FORBIDDEN" for e in value["errors"]):
            raise _GateAuthError(f"integration permission refused on {endpoint.split('?')[0]}")
        raise ValueError("GitHub returned incomplete GraphQL evidence")
    return value


class _GateAuthError(RuntimeError):
    """gh was refused at HTTP 401/403/404 (or GraphQL returned no repository):
    this profile's login cannot see the repo — an identity problem to fix, not
    an infrastructure blip to retry."""


def _gh_env(profile_home: str | None) -> dict[str, str] | None:
    """Child env for ``gh``: the card's profile identity when one is resolvable.

    The completion boundary runs in the worker (assignee), the CLI, or a
    reviewer/dispatcher turn, so an ambient ``gh`` login is whichever process
    happened to call it (#122689). ``served_profile_child_env(inherit_credentials=True)``
    is the seam for "this child acts for that profile": it scrubs the launch
    profile's credential residue and overlays the target profile's own
    ``GH_TOKEN``/``GH_CONFIG_DIR`` (its ``.env`` + external secret sources).
    ``None`` keeps the ambient env — unassigned cards behave exactly as before.
    """
    if not profile_home:
        return None
    from tools.environments.local import _is_routed_home, hermes_subprocess_env, served_profile_child_env
    base = hermes_subprocess_env(inherit_credentials=True)
    routed = _is_routed_home(profile_home)
    if routed:
        # gh's config dir decides which login `gh api` uses, yet it is a path, not a
        # credential, so no scrub list sees it; the target's own value is overlaid from its .env.
        base.pop("GH_CONFIG_DIR", None)
    env = served_profile_child_env(base=base, target_home=profile_home, inherit_credentials=True)
    if not routed and not (env.keys() & {"GH_TOKEN", "GITHUB_TOKEN"}):
        # The target IS the launch profile: hermes_subprocess_env scrubs GH_TOKEN/GITHUB_TOKEN unconditionally and
        # the overlay restores only .env / external-source values, so a token injected by the shell or the service
        # unit (how the gateway itself is logged in) was dropped and the verifier ran unauthenticated: a private
        # repo read "unreadable" and the refusal was silently skipped (Prism d637c235ac85). Our own process's
        # credentials are this profile's credentials; keep them.
        for key in ("GH_TOKEN", "GITHUB_TOKEN"):
            if os.environ.get(key):
                env[key] = os.environ[key]
    if routed and not (env.keys() & {"GH_TOKEN", "GITHUB_TOKEN", "GH_CONFIG_DIR"}):
        # HOME/XDG_CONFIG_HOME are still the launch process's: without a login of its own the
        # child would fall through to ~/.config/gh/hosts.yml — the ambient login. Pin gh's config
        # to a profile-owned dir so it fails "not logged in" (exit 4 -> auth) instead.
        env["GH_CONFIG_DIR"] = str(Path(profile_home) / "gh")
    return env


def _is_human_lane(assignee: str | None) -> bool:
    """``human`` / ``human:<name>``: the review lane's operator sentinel, not a profile."""
    from hermes_cli.kanban_db import is_human_reviewer
    return is_human_reviewer(assignee)


def _assignee_profile_home(assignee: str | None) -> str | None:
    """Home whose ``gh`` login must read the contract repo — the assignee's, resolved
    exactly as the dispatcher resolves the worker's home — or None so the ambient login
    is used: unassigned cards, and the ``human``/``human:<name>`` review lane, which has
    no profile by design and completes as the operator. Any other assigned card whose
    profile cannot be resolved is an identity failure (``auth``), never a silent
    fall-through to the ambient login."""
    if not assignee or _is_human_lane(assignee):
        return None
    from hermes_cli.profiles import normalize_profile_name, resolve_profile_env
    try:
        return resolve_profile_env(normalize_profile_name(assignee))
    except (FileNotFoundError, ValueError):
        raise _GateAuthError(f"assignee profile {assignee!r} cannot be resolved") from None


def collect_acceptance(contract: str, published_pr: str | None,
                       assignee: str | None = None) -> dict:
    receipt = {"ok": False, "classification": "missing", "head_sha": None,
               "pr_url": published_pr, "checks": [],
               "recovery": "Fix required failures, rerun infrastructure checks or wait, then retry completion. "
                           "Use kanban_block if human input is needed; receipts remain on the task event log."}
    human_lane = bool(assignee) and _is_human_lane(assignee)
    if human_lane:
        receipt["login"] = "ambient (human lane)"
    stage = "resolve login"
    try:
        profile_home = _assignee_profile_home(assignee)
        declared = _PR.fullmatch(contract)
        url = contract if declared else published_pr
        match = _PR.fullmatch(url or "")
        if not match or (not declared and match[1] != contract) or (declared and published_pr and published_pr != contract):
            receipt["detail"] = "Supply metadata.published_pr matching the persisted completion contract."
            return receipt
        repo, number = match[1], int(match[2])
        receipt["pr_url"] = url
        owner, name = repo.split("/")
        # Branch protection is read over REST below: GraphQL's branchProtectionRule needs
        # Administration:read, which the fleet's GitHub App token does not hold (t_b5b407c8).
        query = '''{repository(owner:%s,name:%s){pullRequest(number:%d){headRefOid baseRefName state}}}''' % (
                json.dumps(owner), json.dumps(name), number)
        stage = "read pull request (graphql)"
        repository = _api("graphql", query=query, profile_home=profile_home)["data"]["repository"]
        if repository is None:
            # A private repo the login cannot read resolves to null, not an error.
            raise _GateAuthError(f"HTTP 404 on graphql {repo}")
        pr = repository["pullRequest"]
        sha, branch = pr["headRefOid"], pr["baseRefName"]
        receipt["head_sha"] = sha
        if not re.fullmatch(r"[0-9a-f]{40}", sha) or pr["state"] not in {"OPEN", "MERGED"}:
            raise ValueError("PR is closed or current head is unavailable")
        stage = "read branch protection"
        protection = (_api(f"repos/{repo}/branches/{quote(branch, safe='')}",
                           profile_home=profile_home).get("protection") or {})
        checks_cfg = protection.get("required_status_checks") or {}
        required = {(r["context"], r.get("app_id")) for r in checks_cfg.get("checks", [])}
        if set(checks_cfg.get("contexts", [])) - {c for c, _ in required}:
            # A context listed without its ``checks`` entry has an undeterminable app binding;
            # treating it as unpinned would let any producer's same-named check satisfy it.
            raise ValueError("required context without an app binding in branch protection")
        stage = "read branch rulesets"
        rules = _api(f"repos/{repo}/rules/branches/{quote(branch, safe='')}?per_page=100",
                     paginate=True, profile_home=profile_home)
        for page in rules:
            for rule in page:
                if rule["type"] == "required_status_checks":
                    required.update((r["context"], r.get("integration_id"))
                                    for r in rule["parameters"]["required_status_checks"])
        receipt["required"] = [{"context": c, "app_id": a} for c, a in sorted(required, key=str)]
        if not required:
            receipt["detail"] = "No repository-required checks are configured; explicitly use a local-only contract for non-CI tasks."
            return receipt
        stage = "read check runs and statuses"
        pages = _api(f"repos/{repo}/commits/{sha}/check-runs?per_page=100&filter=latest",
                     paginate=True, profile_home=profile_home)
        runs = [run for page in pages for run in page["check_runs"]]
        if len({r["id"] for r in runs}) != pages[0]["total_count"]:
            raise ValueError("Incomplete check-run pagination")
        statuses = [{**s, "sha": sha} for page in _api(f"repos/{repo}/commits/{sha}/statuses?per_page=100",
                                                       paginate=True, profile_home=profile_home) for s in page]
        outcomes = []
        app_ids: dict[str, int | None] = {}
        for context, app_id in sorted(required, key=str):
            matching = [r for r in runs if r["name"] == context and
                        (app_id in (None, -1) or r["app"]["id"] == app_id)]
            # A status satisfies an unpinned context; a pinned context only when the pinned
            # app's own bot posted it (GitHub accepts that; a human/other-app status is the spoof).
            legacy = [s for s in statuses if s["context"] == context and
                      (app_id in (None, -1) or _status_app_id(s, app_ids, profile_home) == app_id)]
            selected = matching + ([max(legacy, key=lambda s: s["id"])] if legacy else [])
            if not selected:
                outcomes.append("missing")
                receipt["checks"].append({"name": context, "classification": "missing", "head_sha": sha})
            for check in selected:
                is_run = "conclusion" in check
                outcome = check.get("conclusion") if is_run else check["state"]
                classification = _classify(check, sha, outcome, is_run)
                outcomes.append(classification)
                entry = {"name": context, "id": check["id"],
                    "url": check.get("html_url") or check.get("target_url"),
                    "head_sha": check.get("head_sha", check.get("sha")),
                    "classification": classification, "conclusion": outcome}
                if not is_run:
                    entry["satisfied_by"] = f"status({(check.get('creator') or {}).get('login')})"
                receipt["checks"].append(entry)
        # Re-read after all pages: old-head successes are never transferable.
        stage = "re-read pull request"
        current = _api(f"repos/{repo}/pulls/{number}", profile_home=profile_home)
        if current["head"]["sha"] != sha or current["base"]["ref"] != branch or (current["state"] == "closed" and not current.get("merged")):
            receipt.update(classification="stale", detail="PR head/base changed while collecting evidence; retry.")
            return receipt
        canonical = ((current.get("base") or {}).get("repo") or {}).get("full_name")
        if canonical and canonical.lower() != repo.lower():
            # Transferred/renamed repo: GitHub resolved the contract's name to this repository.
            # Recorded for audit only; the contract itself is never rewritten.
            receipt["canonical_repo"] = canonical
        if current.get("merged") and "missing" in outcomes and set(outcomes) <= {"success", "missing"}:
            stage = "verify break-glass landing evidence"
            waiver = _break_glass_waiver(repo, canonical or repo, number, sha, current, receipt["checks"], profile_home)
            if waiver:
                receipt["waiver"] = waiver
                for entry in receipt["checks"]:
                    if entry["classification"] == "missing" and entry["name"] in waiver["contexts"]:
                        entry["classification"] = "waived"
                outcomes = ["success" if x == "missing" else x for x in outcomes]
        receipt["classification"] = next((x for x in outcomes if x != "success"), "missing" if not outcomes else "success")
        receipt["ok"] = receipt["classification"] == "success"
        if receipt["classification"] == "missing" and not receipt.get("detail"):
            absent = [c["name"] for c in receipt["checks"] if c["classification"] == "missing"]
            receipt["detail"] = (f"Required checks absent on head {sha[:12]}: {', '.join(absent)}. Run them on this head, "
                                 "or land through fleet-merge --break-glass with a green off-box receipt for this head.")
        return receipt
    except _GateAuthError as exc:
        login = (f"assignee profile {assignee!r}'s gh login" if assignee and not human_lane
                 else "the ambient gh login")
        receipt.update(classification="auth", stage=stage,
                       detail=f"GitHub refused the acceptance read at stage '{stage}' ({exc}) as {login}; "
                              "fix that profile's GitHub credentials/access to the repository, then retry completion.")
        return receipt
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, IndexError) as exc:
        # Never persist gh stderr (credentials/host details); the failed stage + error type are actionable.
        receipt.update(classification="infra", stage=stage, error_type=type(exc).__name__,
                       detail=f"GitHub acceptance evidence unavailable or incomplete at stage '{stage}' "
                              f"({type(exc).__name__}); check gh authentication/API access and retry.")
        return receipt


_RECEIPT_RCS = ("ruff_rc", "pytest_rc", "semgrep_rc", "gitleaks_rc")


def _break_glass_waiver(repo: str, canonical: str, number: int, sha: str, current: dict,
                        checks: list, profile_home: str | None) -> dict | None:
    """Waive required contexts ABSENT on a MERGED head, only on audited break-glass landing evidence.

    Every link is bound to this exact repository/PR/head; anything short of all of them is no
    waiver (the card stays ``missing``):
    - GitHub says the PR is merged at this head, by ``merged_by``;
    - that same account posted the fleet-merge attribution comment (``🤖 merged-by:``) naming
      ``offbox-security-receipt: <path>`` and listing each waived context as a whole
      ``<ctx> (required, ABSENT on head)`` element of ``red-ci allowed [...]``;
    - fleet-merge's landing ledger (``<hermes root>/state/merge-ledger.jsonl``) has a ``merge`` row
      for the canonical OWNER/REPO, this PR and head, naming that same receipt: the receipt file
      carries only the repo basename, so the ledger row is what binds the owner;
    - the receipt lives under ``<hermes root>/var/ci-box/``, names this head and
      ``<repo-name>-<pr>-<head12>``, and every gate rung is rc=0 -- except a red pytest rung when
      that ledger row carries fleet-merge's ``accept_red_receipt`` naming a 40-hex base sha
      (kind ``break-glass-offbox-accepted-red``); ruff/semgrep/gitleaks are never overridable.
    Failed, pending and stale checks are never waived: only absence on an operator break-glass merge.
    """
    from hermes_constants import get_default_hermes_root
    merger = (current.get("merged_by") or {}).get("login")
    if not merger or current["head"]["sha"] != sha:
        return None
    absent = {c["name"] for c in checks if c["classification"] == "missing"}
    if any(" (required, ABSENT on head)" in c["name"] or ")]: " in c["name"] for c in checks):
        # A context name carrying the list's own delimiters makes the attribution list ambiguous.
        return None
    pages = _api(f"repos/{repo}/issues/{number}/comments?per_page=100", paginate=True, profile_home=profile_home)
    hermes_root = Path(get_default_hermes_root())
    root = (hermes_root / "var" / "ci-box").resolve()
    for comment in (c for page in pages for c in page):
        body = comment.get("body") or ""
        if (comment.get("user") or {}).get("login") != merger or not body.startswith("\U0001f916 merged-by:"):
            continue
        path = re.search(r"offbox-security-receipt: (\S+)", body)
        start = body.find("red-ci allowed [")
        if not path or start < 0:
            continue
        # fleet-merge writes "red-ci allowed [<e1>,<e2>]: <reason>"; every element ends in ")",
        # so the list ends at the first ")]: " and the free-text reason can never supply an element.
        allowed = body[start + len("red-ci allowed ["):]
        end = allowed.find(")]: ")
        if end < 0:
            continue
        listed = allowed[:end + 1]
        # An element starts the list or follows the COMPLETE previous element suffix
        # "(required, ABSENT on head),", so punctuation inside another name ("lint,<ctx>",
        # "lint),<ctx>") can never read as a listing of <ctx>.
        if not all(re.search(r"(?:^|\(required, ABSENT on head\),)"
                             + re.escape(ctx + " (required, ABSENT on head)") + r"(?=,|$)", listed)
                   for ctx in absent):
            continue
        evidence = _read_offbox_receipt(Path(path[1]), root, canonical.split("/")[1], number, sha)
        if not evidence:
            continue
        landing = _landing_record(hermes_root / "state" / "merge-ledger.jsonl", canonical, number, sha,
                                  evidence["receipt"])
        if not landing:
            continue
        kind = "break-glass-offbox"
        override = landing.pop("accept_red_receipt", None)
        if "receipt_pytest_rc" in evidence:
            # fleet-merge --accept-red-receipt: a red pytest rung only, overridden on THIS ledger row
            # by text naming the 40-hex base sha where the same red already stands.
            base = re.search(r"(?<![0-9a-f])[0-9a-f]{40}(?![0-9a-f])", override if isinstance(override, str) else "")
            if not base:
                continue
            kind, evidence = "break-glass-offbox-accepted-red", {**evidence, "accepted_red_base": base[0]}
        return {"kind": kind, "merged_by": merger, "comment_id": comment.get("id"),
                "comment_url": comment.get("html_url"), "contexts": sorted(absent), **evidence, **landing}
    return None


def _landing_record(ledger: Path, canonical: str, number: int, sha: str, receipt: str) -> dict | None:
    """fleet-merge's ``merge`` ledger row for exactly OWNER/REPO#number@sha naming ``receipt``."""
    try:
        with ledger.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if sha not in line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if (isinstance(row, dict) and row.get("action") == "merge" and row.get("head") == sha
                        and row.get("pr") == number and str(row.get("repo", "")).lower() == canonical.lower()
                        and _resolved(row.get("receipt_path")) == receipt):
                    return {"ledger_repo": row["repo"], "ledger_attempt_id": row.get("attempt_id"),
                            "ledger_ts": row.get("ts"), "accept_red_receipt": row.get("accept_red_receipt")}
    except OSError:
        return None
    return None


def _resolved(path) -> str | None:
    if not isinstance(path, str) or not path:
        return None
    try:
        return str(Path(path).expanduser().resolve(strict=True))
    except (OSError, RuntimeError):
        return None


def _read_offbox_receipt(path: Path, root: Path, repo_name: str, number: int, sha: str) -> dict | None:
    import hashlib
    try:
        resolved = path.expanduser().resolve(strict=True)
        resolved.relative_to(root)
        raw = resolved.read_bytes()
    except (OSError, RuntimeError, ValueError):
        return None
    fields = {}
    for line in raw.decode("utf-8", "replace").splitlines():
        key, sep, value = line.partition("=")
        if sep and key and not key.startswith(" ") and key not in fields:
            fields[key] = value.strip()
    if fields.get("head") != sha or fields.get("tree") != f"{repo_name}-{number}-{sha[:12]}":
        return None
    # Security rungs are never overridable; a red pytest rung is reported for the caller to bind
    # to a fleet-merge --accept-red-receipt ledger override (fleet-merge.sh step 1a0).
    if any(fields.get(rc) != "0" for rc in _RECEIPT_RCS if rc != "pytest_rc"):
        return None
    evidence = {"receipt": str(resolved), "receipt_sha256": hashlib.sha256(raw).hexdigest()}
    pytest_rc = fields.get("pytest_rc") or ""
    if not pytest_rc.isdigit():
        return None
    if pytest_rc != "0":
        evidence["receipt_pytest_rc"] = pytest_rc
    return evidence


def _status_app_id(status: dict, cache: dict, profile_home: str | None) -> int | None:
    """App id behind a commit status's creator, or None for a human/unresolvable creator.

    Statuses carry no app id; a GitHub App's bot user is ``<app-slug>[bot]`` (type ``Bot``),
    and ``GET /apps/{slug}`` is public, so the slug resolves to the id the rule pins."""
    creator = status.get("creator") or {}
    login = creator.get("login") or ""
    if creator.get("type") != "Bot" or not login.endswith("[bot]"):
        return None
    slug = login[: -len("[bot]")]
    if slug not in cache:
        try:
            cache[slug] = _api(f"apps/{quote(slug, safe='')}", profile_home=profile_home).get("id")
        except (_GateAuthError, subprocess.CalledProcessError):
            cache[slug] = None
    return cache[slug]


def _classify(check: dict, sha: str, outcome: str | None, is_run: bool) -> str:
    if check.get("head_sha", check.get("sha")) != sha:
        return "stale"
    if is_run and check.get("status") != "completed":
        return "pending"
    return {"success": "success", "failure": "failure", "error": "infra", "pending": "pending"}.get(outcome, "infra")
