"""ONE input convention for the kanban lifecycle verbs (Operator-Surface S2, t_cd1bcea4).

Text:        ``--body TEXT`` / ``--body-file PATH`` ('-' = stdin) on every verb that takes prose.
Structured:  ``--json JSON|@PATH|-`` on every verb that takes a JSON object (complete /
             request-review metadata, request-changes coverage). ``@PATH`` and ``-`` keep the
             shell away from the payload.
Refusals:    ``--why`` prints what would refuse this call and the command that clears it, then
             exits 0 without acting. Every input refusal prints the verb's FULL schema and an
             example (:func:`schema_text`), never one field.
Coverage:    ``request-changes --finding TEXT`` (repeatable) + ``--lens NAME=STATE`` BUILD the
             review_coverage JSON (findings = count, items, review_minutes from the review run's
             clock, head_sha from the card's routed PR, batch_id). The JSON is an output.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Optional

from hermes_cli import kanban_review_schema as rs

# verb -> (required fields, optional fields, example). The field names are the ones the
# refusal must name (tests drive every verb with empty/partial input against this table).
VERB_SCHEMAS: dict = {
    "create": (("title",), ("--body|--body-file", "--assignee", "--parent"),
               'hermes kanban create "<title>" --assignee <profile> --body-file - <<\'EOF\' ... EOF'),
    "comment": (("task_id", "body"), ("--author",),
                'hermes kanban comment <task_id> --body "<text>"   (or --body-file PATH|-)'),
    "complete": (("task_id", "summary"), ("--json <metadata object>", "--survivor-pr", "--superseded-by",
                                          "--result"),
                 'hermes kanban complete <task_id> --body "<what was done + evidence>" '
                 '--json \'{"pr_url": "https://github.com/o/r/pull/N"}\''),
    "block": (("task_id",), ("reason (--body|--body-file|positional)", "--kind", "--until"),
              'hermes kanban block <task_id> --body "<the one decision you need>" --kind needs_input'),
    "request-review": (("task_id",), ("summary (--body|--body-file|--summary)", "--json <metadata object>",
                                      "--reviewer"),
                       'hermes kanban request-review <task_id> --body "<what changed, how verified>"'),
    "request-changes": (("task_id", "reason", "coverage") + ("lenses",) + rs.REQUIRED_COVERAGE_FIELDS,
                        ("--operator \"<who: why>\" (waives coverage)",),
                        'hermes kanban request-changes <task_id> --body "<what to change>" '
                        '--finding "<finding 1>" --finding "<finding 2>" --lens all=done'),
}


def schema_text(verb: str) -> str:
    """The verb's whole contract in one block: required, optional, example (+ coverage schema)."""
    required, optional, example = VERB_SCHEMAS[verb]
    text = (f"kanban {verb} input: required {', '.join(required)}; optional {', '.join(optional)}.\n"
            f"  example: {example}")
    if verb == "request-changes":
        text += "\n  " + rs.coverage_schema_text()
    return text


def refuse(verb: str, message: str, rc: int = 2) -> int:
    print(f"kanban {verb}: {message}\n{schema_text(verb)}", file=sys.stderr)
    return rc


def _read(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    return Path(path).expanduser().read_text(encoding="utf-8-sig")


def resolve_text(args, verb: str, *, words_attr: Optional[str] = None,
                 alias_attr: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
    """``(text, error)`` from ``--body`` / ``--body-file`` / positional words / a legacy alias flag.

    Exactly one source may be given; none is ``(None, None)`` (the caller decides if text is
    required). ``alias_attr`` is the verb's historic flag (``--summary``) kept for compat.
    """
    sources = []
    body = getattr(args, "body", None)
    if body is not None:
        sources.append(("--body", body))
    body_file = getattr(args, "body_file", None)
    if body_file is not None:
        try:
            sources.append(("--body-file", _read(body_file)))
        except OSError as exc:
            return None, f"--body-file: {exc}"
    words = getattr(args, words_attr, None) if words_attr else None
    if words:
        sources.append(("positional text", " ".join(words)))
    alias = getattr(args, alias_attr, None) if alias_attr else None
    if alias is not None:
        sources.append((f"--{alias_attr.replace('_', '-')}", alias))
    if len(sources) > 1:
        return None, ("text given twice (" + " and ".join(name for name, _ in sources)
                      + "); pass it ONE way")
    if not sources:
        return None, None
    return sources[0][1].strip(), None


def resolve_json(value: Optional[str], flag: str = "--json") -> tuple[Optional[dict], Optional[str]]:
    """``(object, error)`` from inline JSON, ``@PATH`` or ``-`` (stdin)."""
    if value is None:
        return None, None
    raw = value
    try:
        if value == "-":
            raw = sys.stdin.read()
        elif value.startswith("@"):
            raw = _read(value[1:])
    except OSError as exc:
        return None, f"{flag} {value}: {exc}"
    try:
        obj = json.loads(raw)
    except ValueError as exc:
        pos = getattr(exc, "pos", None)
        near = f" near {raw[max(0, pos - 30):pos + 10]!r}" if isinstance(pos, int) else ""
        return None, (f"{flag} is not valid JSON ({getattr(exc, 'msg', exc)}{near}); "
                      f"pass {flag} @FILE or {flag} - so the shell never rewrites it")
    if not isinstance(obj, dict):
        return None, f"{flag} must be a JSON object, got {type(obj).__name__}"
    return obj, None


# ------------------------------------------------------------------ coverage builder
def _lens_states(specs: list) -> tuple[dict, list]:
    lenses: dict = {}
    errors: list = []
    for spec in specs or []:
        name, sep, state = str(spec).partition("=")
        name, state = name.strip(), state.strip()
        if not sep or not name or not state:
            errors.append(f"--lens {spec!r}: want NAME=STATE (e.g. contract=done, all=done, "
                          f"mutation='n/a: docs-only change')")
            continue
        if name == "all":
            lenses.update({lens: state for lens in rs.REQUIRED_REVIEW_LENSES if lens not in lenses})
        else:
            lenses[name] = state
    return lenses, errors


def _routed_pr_head(conn, task_id: str) -> tuple[Optional[str], Optional[str]]:
    """``(head_sha, pr)`` of the card's routed/declared PR; ``(None, None)`` when it has none,
    ``(None, pr)`` when it has one whose head could not be read."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_open_pr import extract_pr_refs, split_fleet

    refs = []
    for run in reversed(kb.list_runs(conn, task_id)):
        md = run.metadata if isinstance(run.metadata, dict) else None
        refs = split_fleet(extract_pr_refs(metadata=md))[0]
        if refs:
            break
    if not refs:
        return None, None
    ref = refs[0]
    pr = f"{ref.repo}#{ref.number}"
    from hermes_cli import kanban_handback_head as hh

    reader = hh._default_reader()
    data = reader(ref.repo, ref.number) if reader else None
    head = (data or {}).get("head")
    return (head if isinstance(head, str) and head else None), pr


def _review_minutes(conn, task_id: str) -> int:
    """Minutes since the active review run opened (0 when none is open: the parked send-back
    opens it in the same call)."""
    from hermes_cli import kanban_db as kb

    task = kb.get_task(conn, task_id)
    if task is None or task.status != "running" or task.current_run_id is None:
        return 0
    run = kb.get_run(conn, task.current_run_id)
    if run is None or not run.started_at:
        return 0
    return max(0, min(kb._REVIEW_MINUTES_MAX, int((time.time() - run.started_at) // 60)))


def build_coverage(conn, task_id: str, *, findings: list, lens_specs: list,
                   overrides: Optional[dict]) -> tuple[Optional[str], Optional[str]]:
    """``(coverage_json, error)``: the flat form, computed defaults, ``--json`` keys win."""
    lenses, errors = _lens_states(lens_specs)
    overrides = dict(overrides or {})
    record: dict = {}
    if lenses or "lenses" not in overrides:
        record["lenses"] = lenses
    items = [str(f).strip() for f in findings or [] if str(f).strip()]
    if items:
        record["findings"], record["items"] = len(items), items
    if "head_sha" not in overrides:
        head, pr = _routed_pr_head(conn, task_id)
        if head:
            record["head_sha"] = head
        elif pr is None:
            record["head_sha"] = "n/a: card routes no PR"
        else:
            errors.append(f"head_sha: could not read the head of {pr} (gh REST); "
                          f"pass --json '{{\"head_sha\": \"<sha>\"}}'")
    if "review_minutes" not in overrides:
        record["review_minutes"] = _review_minutes(conn, task_id)
    record.setdefault("batch_id", "n/a: request-changes flat form, no delegate_task batch")
    if isinstance(overrides.get("lenses"), dict) and lenses:
        overrides["lenses"] = {**overrides["lenses"], **lenses}
    record.update(overrides)
    if errors:
        return None, "; ".join(errors)
    return json.dumps(record, ensure_ascii=False), None


# ------------------------------------------------------------------ --why
def why_report(conn, verb: str, task_id: Optional[str], problems: list) -> str:
    """One paragraph: card state, every input problem, what clears it. Read-only."""
    from hermes_cli import kanban_db as kb

    lines = []
    allowed = {
        "complete": ("running", "ready", "blocked", "review"),
        "block": ("running", "ready"),
        "request-review": ("running", "ready"),
        "request-changes": ("review", "running"),
        "comment": None,
    }.get(verb)
    if task_id:
        task = kb.get_task(conn, task_id)
        if task is None:
            lines.append(f"{task_id}: unknown id on this board (wrong --board?)")
        else:
            lines.append(f"{task_id}: status={task.status} assignee={task.assignee or '-'} "
                         f"claim={'held' if task.claim_lock else 'none'}")
            if allowed is not None and task.status not in allowed:
                lines.append(f"blocked: {verb} needs status in {', '.join(allowed)}")
            if verb == "complete":
                open_parents = kb.unsatisfied_parents(conn, task_id)
                if open_parents:
                    lines.append("blocked: open parents " + ", ".join(f"{p} ({s})" for p, s in open_parents)
                                 + "; clear with: hermes kanban unlink <parent> " + task_id
                                 + " (or finish the parent)")
            if verb == "request-changes" and task.status == "review":
                lines.append("note: parked in review; the send-back opens the review run in the same "
                             "call (your session, or --operator \"<who: why>\" / --takeover REASON)")
    for problem in problems:
        lines.append(f"input: {problem}")
    if not problems and not any(line.startswith("blocked:") for line in lines):
        lines.append("ok: no refusal predicted from card state and input (the write path re-checks "
                     "ownership, receipts and PR gates)")
    lines.append(schema_text(verb))
    return "\n".join(lines)
