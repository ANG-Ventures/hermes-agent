"""``launchctl bootstrap`` of an EXISTING non-gateway plist must be ALLOWED.

``contains_launchctl_submit_command`` treats ``submit`` and ``bootstrap``
label-INDEPENDENTLY because a NEW job's label is chosen by whoever writes the
command (#62891). That is correct for ``submit`` (the label is pure text) and
for ``bootstrap`` of a path that does not exist yet, but it is wrong for
``bootstrap`` of a plist that is ALREADY on disk: launchd reads the ``Label``
key out of that file, so the label is not attacker-chosen at all — it is a
readable fact about the job that will be registered.

Measured 2026-09-20 inside the supervised default gateway on the Studio:
reloading the fleetreview router after a ``plutil -replace`` edit —

    sudo launchctl bootout system/ai.hermes.fleetreview-router
    sudo launchctl bootstrap system /Library/LaunchDaemons/ai.hermes.fleetreview-router.plist

— was refused with the gateway-lifecycle block even though
``ai.hermes.fleetreview-router`` is not a gateway label. The workaround used
was the legacy, label-gated ``launchctl load -w``; that is a gap, not a design.

Everything that cannot be READ stays blocked: a non-existent path, an
unreadable/oversized/malformed file, a path carrying an unexpanded shell
value, a plist whose ``Label`` IS a gateway label, a plist whose
``ProgramArguments`` invoke a hermes gateway entrypoint, and ``submit`` in
every shape.
"""

from __future__ import annotations

import os
import pathlib
import plistlib

import pytest

from cron import lifecycle_guard
from cron.lifecycle_guard import (
    contains_gateway_lifecycle_command_or_referenced_script,
    contains_launchctl_submit_command,
)


SELF_LAUNCHD = "ai.hermes.gateway-aegis"


@pytest.fixture
def launchd_identity(monkeypatch):
    """Pin self-identity to the Aegis launchd job, as launchd would."""
    monkeypatch.setenv("XPC_SERVICE_NAME", SELF_LAUNCHD)
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    monkeypatch.setattr(lifecycle_guard, "_profile_derived_self_names", lambda: set())
    return SELF_LAUNCHD


@pytest.fixture
def no_identity(monkeypatch):
    """No determinable identity at all — the guard must fail closed."""
    monkeypatch.delenv("XPC_SERVICE_NAME", raising=False)
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    monkeypatch.setattr(lifecycle_guard, "_profile_derived_self_names", lambda: set())


def _write_plist(path, payload) -> str:
    """Write *payload* as a binary plist and return its path as a string."""
    with open(path, "wb") as handle:
        plistlib.dump(payload, handle, fmt=plistlib.FMT_BINARY)
    return str(path)


@pytest.fixture
def router_plist(tmp_path):
    """The real fleetreview-router plist shape, minus the irrelevant keys."""
    return _write_plist(
        tmp_path / "ai.hermes.fleetreview-router.plist",
        {
            "Label": "ai.hermes.fleetreview-router",
            "ProgramArguments": [
                "/usr/local/libexec/fleetreview-router/venv/bin/"
                "fleetreview-router-service",
                "--live",
            ],
            "RunAtLoad": True,
        },
    )


# ---------------------------------------------------------------------------
# POSITIVE CONTROL — today's exact command shape must be ALLOWED
# ---------------------------------------------------------------------------


class TestExistingNonGatewayPlistAllowed:
    def test_incident_command_shape_allowed(self, router_plist, launchd_identity):
        """The 2026-09-20 reload that was refused."""
        text = f"sudo launchctl bootstrap system {router_plist}"
        assert not contains_launchctl_submit_command(text)
        assert not contains_gateway_lifecycle_command_or_referenced_script(text)

    def test_bootout_then_bootstrap_sequence_allowed(
        self, router_plist, launchd_identity
    ):
        """Both halves of the reload, as actually typed."""
        text = (
            "sudo launchctl bootout system/ai.hermes.fleetreview-router && "
            f"sudo launchctl bootstrap system {router_plist}"
        )
        assert not contains_launchctl_submit_command(text)
        assert not contains_gateway_lifecycle_command_or_referenced_script(text)

    def test_gui_domain_form_allowed(self, router_plist, launchd_identity):
        text = f"launchctl bootstrap gui/501 {router_plist}"
        assert not contains_launchctl_submit_command(text)

    def test_quoted_path_allowed(self, tmp_path, launchd_identity):
        path = _write_plist(
            tmp_path / "ai.hermes.cert watch.plist",
            {"Label": "ai.hermes.cert-watch", "ProgramArguments": ["/bin/true"]},
        )
        text = f'launchctl bootstrap system "{path}"'
        assert not contains_launchctl_submit_command(text)

    def test_allowed_without_self_identity(self, router_plist, no_identity):
        """A non-gateway Label is safe even when we cannot name ourselves.

        The sibling-plist path needs self-identity (it compares labels); this
        one does not — a Label that is not a gateway label at all cannot be
        OUR gateway regardless of which gateway we are.
        """
        text = f"launchctl bootstrap system {router_plist}"
        assert not contains_launchctl_submit_command(text)


# ---------------------------------------------------------------------------
# NEGATIVE CONTROLS — everything unreadable or gateway-ish stays BLOCKED
# ---------------------------------------------------------------------------


class TestBootstrapStillBlocked:
    def test_gateway_label_plist_blocked(self, tmp_path, launchd_identity):
        """A plist whose Label IS our own gateway label."""
        path = _write_plist(
            tmp_path / "some-name.plist",
            {"Label": SELF_LAUNCHD, "ProgramArguments": ["/bin/true"]},
        )
        text = f"launchctl bootstrap gui/501 {path}"
        assert contains_launchctl_submit_command(text)
        assert contains_gateway_lifecycle_command_or_referenced_script(text)

    def test_new_file_gateway_suffix_label_blocked(self, tmp_path, launchd_identity):
        """Filename is neutral, Label is `ai.hermes.gateway-foo` — blocked.

        The file basename is deliberately NOT a gateway label, so only reading
        the Label key can catch this.
        """
        path = _write_plist(
            tmp_path / "reload-helper.plist",
            {"Label": "ai.hermes.gateway-foo", "ProgramArguments": ["/bin/true"]},
        )
        text = f"launchctl bootstrap gui/501 {path}"
        assert contains_launchctl_submit_command(text)

    def test_gateway_entrypoint_in_program_arguments_blocked(
        self, tmp_path, launchd_identity
    ):
        """Neutral Label, but the job runs a hermes gateway."""
        path = _write_plist(
            tmp_path / "ai.hermes.svc-reload-tmp.plist",
            {
                "Label": "ai.hermes.svc-reload-tmp",
                "ProgramArguments": [
                    "/Users/ace/.hermes/runtime/hermes-agent/venv/bin/python",
                    "-m",
                    "hermes_cli.main",
                    "gateway",
                    "run",
                    "--replace",
                ],
            },
        )
        text = f"launchctl bootstrap gui/501 {path}"
        assert contains_launchctl_submit_command(text)

    def test_gateway_entrypoint_in_program_blocked(self, tmp_path, launchd_identity):
        """Same, via the scalar ``Program`` key rather than the argv list."""
        path = _write_plist(
            tmp_path / "ai.hermes.svc-other.plist",
            {
                "Label": "ai.hermes.svc-other",
                "Program": "/Users/ace/.hermes/bin/hermes-gateway-launcher",
            },
        )
        text = f"launchctl bootstrap gui/501 {path}"
        assert contains_launchctl_submit_command(text)

    def test_gateway_entrypoint_split_across_argv_blocked(
        self, tmp_path, launchd_identity
    ):
        """Neutral Label, no marker token — `hermes` and `gateway` as
        SEPARATE argv words.

        A bare launcher (`/usr/local/bin/hermes gateway run`) contains none
        of ``_PLIST_GATEWAY_ARGV_MARKERS``; only the split-token rule sees it.
        """
        path = _write_plist(
            tmp_path / "ai.hermes.svc-bare.plist",
            {
                "Label": "ai.hermes.svc-bare",
                "ProgramArguments": ["/usr/local/bin/hermes", "gateway", "run"],
            },
        )
        text = f"launchctl bootstrap gui/501 {path}"
        assert contains_launchctl_submit_command(text)

    def test_argv_runs_gateway_lifecycle_inline_blocked(
        self, tmp_path, launchd_identity
    ):
        """Neutral Label, non-entrypoint argv — but it KICKSTARTS us.

        Review round 1's finding: "is this plist a gateway job?" is not the
        question the guard exists to answer. This plist is not a gateway job
        by any tell (Label is neutral, argv is ``/bin/sh``), yet loading it
        restarts THIS gateway — the #62891 laundering shape with a file
        instead of a ``submit`` line. Needs no root: a user-writable
        ``$TMPDIR`` path bootstrapped into ``gui/<uid>`` is enough.
        """
        path = _write_plist(
            tmp_path / "ai.hermes.helper.plist",
            {
                "Label": "ai.hermes.helper",
                "ProgramArguments": [
                    "/bin/sh",
                    "-c",
                    f"launchctl kickstart -k system/{SELF_LAUNCHD}",
                ],
            },
        )
        text = f"launchctl bootstrap gui/501 {path}"
        assert contains_launchctl_submit_command(text)
        assert contains_gateway_lifecycle_command_or_referenced_script(text)

    def test_argv_runs_gateway_lifecycle_via_script_blocked(
        self, tmp_path, launchd_identity
    ):
        """Same laundering, one indirection deeper: argv names a SCRIPT.

        The inline-string witness alone would pass a fix that only scanned
        ``sh -c`` payloads; the lifecycle scanner's referenced-script walk is
        what closes this one.
        """
        script = tmp_path / "boot.sh"
        script.write_text(
            f"#!/bin/sh\nlaunchctl bootout system/{SELF_LAUNCHD}\n", encoding="utf-8"
        )
        script.chmod(0o755)
        path = _write_plist(
            tmp_path / "ai.hermes.helper2.plist",
            {
                "Label": "ai.hermes.helper2",
                "ProgramArguments": ["/bin/sh", str(script)],
            },
        )
        text = f"launchctl bootstrap gui/501 {path}"
        assert contains_launchctl_submit_command(text)
        assert contains_gateway_lifecycle_command_or_referenced_script(text)

    def test_argv_runs_gateway_lifecycle_direct_argv_blocked(
        self, tmp_path, launchd_identity
    ):
        """The lifecycle command IS the argv — no ``sh -c``, no script.

        ``ProgramArguments`` is already a split command line, so no single
        token contains a lifecycle command; only scanning the JOINED argv
        sees it. Without this witness a fix that scans tokens individually
        passes every other negative here.
        """
        path = _write_plist(
            tmp_path / "ai.hermes.direct.plist",
            {
                "Label": "ai.hermes.direct",
                "ProgramArguments": [
                    "/bin/launchctl",
                    "kickstart",
                    "-k",
                    f"system/{SELF_LAUNCHD}",
                ],
            },
        )
        text = f"launchctl bootstrap gui/501 {path}"
        assert contains_launchctl_submit_command(text)
        assert contains_gateway_lifecycle_command_or_referenced_script(text)

    def test_argv_bootstraps_another_plist_blocked(self, tmp_path, monkeypatch, launchd_identity):
        """A plist whose argv bootstraps a plist re-enters the argv scan.

        Two plists can reference each other; the scan is depth-bounded and
        fails CLOSED at the bound, so the cycle terminates as a refusal.

        The verdict alone does NOT gate the bound — measured: with the bound
        removed the cycle blows the Python stack, and the guard's own
        ``except Exception`` fallback still returns the same ``True``. So
        assert the WORK done: a bounded scan reads a handful of plists, an
        unbounded one read 116 before the interpreter gave out.
        """
        first = tmp_path / "ai.hermes.chain-a.plist"
        second = tmp_path / "ai.hermes.chain-b.plist"
        _write_plist(
            first,
            {
                "Label": "ai.hermes.chain-a",
                "ProgramArguments": [
                    "/bin/sh",
                    "-c",
                    f"launchctl bootstrap gui/501 {second}",
                ],
            },
        )
        _write_plist(
            second,
            {
                "Label": "ai.hermes.chain-b",
                "ProgramArguments": [
                    "/bin/sh",
                    "-c",
                    f"launchctl bootstrap gui/501 {first}",
                ],
            },
        )
        real_read = lifecycle_guard._read_plist_label_payload
        reads = []

        def counting_read(path):
            reads.append(path)
            return real_read(path)

        monkeypatch.setattr(
            lifecycle_guard, "_read_plist_label_payload", counting_read
        )
        assert contains_launchctl_submit_command(
            f"launchctl bootstrap gui/501 {first}"
        )
        assert len(reads) <= 8, f"unbounded plist recursion: {len(reads)} reads"
        # The per-thread depth counter must unwind to 0, or the NEXT scan in
        # this process starts pre-charged and fails closed on a benign plist.
        assert getattr(lifecycle_guard._PLIST_ARGV_SCAN_STATE, "depth", 0) == 0

    def test_nonexistent_path_blocked(self, tmp_path, launchd_identity):
        """Nothing to read → the label is still attacker-chosen (#62891)."""
        text = f"launchctl bootstrap gui/501 {tmp_path / 'not-written-yet.plist'}"
        assert contains_launchctl_submit_command(text)

    def test_variable_in_path_blocked(self, launchd_identity):
        """Unexpanded shell value: the path we read is not the path that runs."""
        text = "launchctl bootstrap gui/501 $PLIST"
        assert contains_launchctl_submit_command(text)

    def test_variable_in_domain_argument_blocked(self, router_plist, launchd_identity):
        """The plist is real and benign, but ANOTHER argument is unexpanded.

        `launchctl bootstrap gui/$UID <plist>` is the common spelling. The
        shell-value check covers every argument, not just the plist paths —
        a variable elsewhere in the segment still means we are reasoning
        about a command whose expanded form we have not seen.
        """
        text = f"launchctl bootstrap gui/$UID {router_plist}"
        assert contains_launchctl_submit_command(text)

    def test_variable_inside_existing_path_blocked(self, router_plist, launchd_identity):
        """A real file plus a variable segment is still unresolvable."""
        text = f"launchctl bootstrap gui/501 ${{DIR}}/{router_plist}"
        assert contains_launchctl_submit_command(text)

    def test_command_substitution_in_path_blocked(self, launchd_identity):
        text = "launchctl bootstrap gui/501 $(mktemp).plist"
        assert contains_launchctl_submit_command(text)

    def test_directory_argument_blocked(self, tmp_path, launchd_identity):
        """`bootstrap <domain> <dir>` loads every plist in the directory."""
        directory = tmp_path / "agents"
        directory.mkdir()
        _write_plist(
            directory / "ai.hermes.gateway-aegis.plist",
            {"Label": SELF_LAUNCHD, "ProgramArguments": ["/bin/true"]},
        )
        text = f"launchctl bootstrap gui/501 {directory}"
        assert contains_launchctl_submit_command(text)

    def test_fifo_argument_blocked(self, tmp_path, launchd_identity):
        """A FIFO named `.plist` must be refused WITHOUT being read.

        Unlike a directory (whose ``os.read`` raises ``IsADirectoryError``
        and is caught downstream), an unopened-for-write FIFO would make the
        read block. ``O_NONBLOCK`` keeps the open from hanging, but only the
        ``S_ISREG`` check makes the refusal explicit rather than incidental.
        """
        path = tmp_path / "ai.hermes.fifo.plist"
        os.mkfifo(path)
        assert lifecycle_guard._read_plist_label_payload(path) is None
        text = f"launchctl bootstrap gui/501 {path}"
        assert contains_launchctl_submit_command(text)

    def test_directory_named_like_a_plist_blocked(self, tmp_path, launchd_identity):
        """A DIRECTORY whose name ends in `.plist` is not a readable job."""
        path = tmp_path / "ai.hermes.dir.plist"
        path.mkdir()
        assert lifecycle_guard._read_plist_label_payload(path) is None
        text = f"launchctl bootstrap gui/501 {path}"
        assert contains_launchctl_submit_command(text)

    def test_unparseable_file_blocked(self, tmp_path, launchd_identity):
        """A file that is not a plist at all cannot clear the guard."""
        path = tmp_path / "ai.hermes.broken.plist"
        path.write_text("this is not a plist\n")
        text = f"launchctl bootstrap gui/501 {path}"
        assert contains_launchctl_submit_command(text)

    def test_plist_without_label_blocked(self, tmp_path, launchd_identity):
        path = _write_plist(
            tmp_path / "ai.hermes.nolabel.plist",
            {"ProgramArguments": ["/bin/true"]},
        )
        text = f"launchctl bootstrap gui/501 {path}"
        assert contains_launchctl_submit_command(text)

    def test_oversized_file_blocked(self, tmp_path, launchd_identity):
        """Bounded read: anything over the cap is refused, not streamed."""
        path = tmp_path / "ai.hermes.huge.plist"
        payload = {
            "Label": "ai.hermes.huge",
            "ProgramArguments": ["/bin/true"],
            "Padding": "x" * (lifecycle_guard._MAX_PLIST_BYTES + 1024),
        }
        _write_plist(path, payload)
        assert path.stat().st_size > lifecycle_guard._MAX_PLIST_BYTES
        text = f"launchctl bootstrap gui/501 {path}"
        assert contains_launchctl_submit_command(text)

    def test_oversized_file_reader_returns_none(self, tmp_path):
        """Direct unit gate on the reader, independent of the verb path.

        ``_read_plist_label_payload`` carries THREE overlapping bounds (an
        ``st_size`` pre-check, a bounded read loop, and a post-read length
        check). Any one of them alone produces this ``None``, so no single
        mutation of them can be killed — this asserts the property they
        jointly guarantee rather than any one implementation of it.
        """
        path = tmp_path / "ai.hermes.huge.plist"
        _write_plist(
            path,
            {
                "Label": "ai.hermes.huge",
                "Padding": "x" * (lifecycle_guard._MAX_PLIST_BYTES + 1024),
            },
        )
        assert path.stat().st_size > lifecycle_guard._MAX_PLIST_BYTES
        assert lifecycle_guard._read_plist_label_payload(path) is None

    def test_reader_accepts_a_normal_plist(self, router_plist):
        """Positive control for the reader: the bound is not a blanket refusal."""
        payload = lifecycle_guard._read_plist_label_payload(
            pathlib.Path(router_plist)
        )
        assert payload is not None
        assert payload["Label"] == "ai.hermes.fleetreview-router"

    def test_mixed_readable_and_unreadable_blocked(self, router_plist, launchd_identity):
        """Every plist argument must clear; one unreadable one blocks all."""
        text = (
            f"launchctl bootstrap gui/501 {router_plist} "
            "/tmp/definitely-not-here-9f2c.plist"
        )
        assert contains_launchctl_submit_command(text)

    def test_no_plist_argument_blocked(self, launchd_identity):
        text = "launchctl bootstrap gui/501"
        assert contains_launchctl_submit_command(text)


class TestSubmitUnchanged:
    @pytest.mark.parametrize(
        "text",
        [
            "launchctl submit -l ai.hermes.gateway -- /bin/sh helper.sh",
            "launchctl submit -l ai.hermes.fleetreview-router -- /bin/sh helper.sh",
            "launchctl submit -l neutral-name -- /bin/sh helper.sh",
        ],
    )
    def test_submit_blocked_regardless_of_label(self, text, launchd_identity):
        """`submit` never reads a file — its label proves nothing."""
        assert contains_launchctl_submit_command(text), f"Should match: {text!r}"
        assert contains_gateway_lifecycle_command_or_referenced_script(text)

    def test_submit_with_a_readable_plist_argument_still_blocked(
        self, router_plist, launchd_identity
    ):
        """Naming a benign plist must not launder a `submit`."""
        text = f"launchctl submit -l tmp -- /bin/cat {router_plist}"
        assert contains_launchctl_submit_command(text)


class TestSiblingGatewayPlistStillAllowed:
    """The pre-existing sibling-gateway exemption must survive unchanged."""

    def test_sibling_gateway_plist_by_name_allowed(self, launchd_identity):
        text = (
            "launchctl bootstrap gui/501 "
            "/Users/ace/Library/LaunchAgents/ai.hermes.gateway.plist"
        )
        assert not contains_launchctl_submit_command(text)

    def test_self_gateway_plist_by_name_blocked(self, launchd_identity):
        text = (
            "launchctl bootstrap gui/501 "
            f"/Users/ace/Library/LaunchAgents/{SELF_LAUNCHD}.plist"
        )
        assert contains_launchctl_submit_command(text)
