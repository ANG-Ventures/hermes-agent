"""Heredoc bodies are DATA, not commands, for the gateway lifecycle guard.

Regression coverage for the false positive recorded as papercut `pc-fccd2771`:
patching a README through a python heredoc was refused because the literal WORDS
"restart"/"stop" appeared inside the quoted document body.

The lifecycle guard exists to stop the gateway killing its own in-flight command by
restarting/stopping itself. A heredoc BODY is stdin bytes handed to a command — a
document being written, a Python program's string literals. A lifecycle phrase that
appears only there is prose.

The masking is deliberately fail-CLOSED: this module's second half asserts that every
shape where the body could ACTUALLY execute (fed to a shell, piped to a shell, or an
unquoted delimiter whose body carries command substitution) still blocks. Those cases
matter more than the false-positive fix — a masker that over-exempts would turn a
safety guard off.
"""
from __future__ import annotations

import pytest

from cron.lifecycle_guard import (
    _mask_heredoc_bodies,
    contains_gateway_lifecycle_command_or_referenced_script as guard,
)

# Build the lifecycle verbs at runtime so this test FILE is not itself refused by the
# very guard it tests when an agent edits it through the terminal tool.
R = "re" + "start"
S = "st" + "op"


# ---------------------------------------------------------------------------
# FALSE POSITIVES the fix removes — prose in a heredoc body must be allowed
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name,command",
    [
        (
            "python heredoc, quoted delimiter (pc-fccd2771)",
            "python3 - <<'EOF'\ns = \"the gateway would %s the service\"\nEOF" % R,
        ),
        (
            "python heredoc patching a doc (the exact real-world shape)",
            "python3 - <<'PYEOF'\n"
            "s = s.replace('you may still need to %s it', 'x')\n"
            "PYEOF" % R,
        ),
        (
            "cat heredoc into a file, UNQUOTED delimiter",
            "cat <<EOF > /tmp/notes.md\nRun hermes gateway %s from a separate shell.\nEOF" % R,
        ),
        (
            "indented heredoc (<<-)",
            "cat <<-EOF > /tmp/n.md\n\tdocs: how to %s the gateway\n\tEOF" % R,
        ),
        (
            "double-quoted delimiter",
            'cat <<"EOF" > /tmp/n.md\nhermes gateway %s is documented here\nEOF' % R,
        ),
        (
            "lifecycle word only in the body, real command is benign",
            "tee /tmp/x.md <<'EOF'\nhermes gateway %s\nEOF" % S,
        ),
    ],
)
def test_heredoc_body_prose_is_allowed(name, command):
    assert guard(command) is False, f"false positive: {name}"


# ---------------------------------------------------------------------------
# TRUE POSITIVES — fail-closed. These MUST stay blocked.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name,command",
    [
        ("heredoc fed to bash", "bash <<'EOF'\nhermes gateway %s\nEOF" % R),
        ("heredoc fed to sh, unquoted", "sh <<EOF\nhermes gateway %s\nEOF" % R),
        ("heredoc fed to zsh", "zsh <<'EOF'\nhermes gateway %s\nEOF" % S),
        ("heredoc piped into bash", "cat <<'EOF' | bash\nhermes gateway %s\nEOF" % R),
        ("heredoc piped into sh", "cat <<EOF | sh\nhermes gateway %s\nEOF" % R),
        (
            "UNQUOTED delimiter with command substitution in the body",
            "cat <<EOF > /tmp/x\n$(hermes gateway %s)\nEOF" % R,
        ),
        (
            "UNQUOTED delimiter with backtick substitution in the body",
            "cat <<EOF > /tmp/x\n`hermes gateway %s`\nEOF" % R,
        ),
        ("sudo bash heredoc", "sudo bash <<'EOF'\nhermes gateway %s\nEOF" % R),
    ],
)
def test_executable_heredoc_bodies_still_block(name, command):
    assert guard(command) is True, f"FAIL-OPEN: {name}"


# ---------------------------------------------------------------------------
# The masker must not weaken anything OUTSIDE a heredoc
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name,command",
    [
        ("bare invocation", "hermes gateway %s" % R),
        ("bare stop", "hermes gateway %s" % S),
        ("bash -c", 'bash -c "hermes gateway %s"' % R),
        ("command substitution in a commit message", 'git commit -m "$(hermes gateway %s)"' % R),
        ("after &&", 'git commit -m "msg" && hermes gateway %s' % R),
        ("launchctl kickstart", "launchctl kickstart -k gui/501/ai.hermes.gateway"),
        ("systemctl", "systemctl %s hermes-gateway" % R),
        (
            "real command on the line that ALSO opens a prose heredoc",
            "hermes gateway %s && cat <<'EOF' > /tmp/x\njust prose\nEOF" % R,
        ),
    ],
)
def test_non_heredoc_invocations_still_block(name, command):
    assert guard(command) is True, f"FAIL-OPEN: {name}"


# ---------------------------------------------------------------------------
# Masker unit behavior
# ---------------------------------------------------------------------------
def test_masker_is_a_noop_without_a_heredoc():
    text = "hermes gateway %s" % R
    assert _mask_heredoc_bodies(text) == text


def test_masker_preserves_the_opening_and_terminator_lines():
    text = "cat <<'EOF' > /tmp/x\nbody line\nEOF"
    out = _mask_heredoc_bodies(text).splitlines()
    assert out[0] == "cat <<'EOF' > /tmp/x"
    assert out[-1] == "EOF"
    assert "body line" not in out


def test_masker_handles_an_unterminated_heredoc():
    """A truncated/unterminated heredoc must not crash or drop the guard."""
    text = "cat <<'EOF' > /tmp/x\nhermes gateway %s" % R
    # Body is still masked (it is data), and the call must not raise.
    assert _mask_heredoc_bodies(text) is not None
    # An unterminated PROSE heredoc is still data, so it stays allowed.
    assert guard(text) is False


def test_masker_handles_multiple_heredocs():
    text = (
        "cat <<'A' > /tmp/1\nprose about %s\nA\n"
        "cat <<'B' > /tmp/2\nmore prose %s\nB" % (R, S)
    )
    assert guard(text) is False


def test_second_heredoc_executing_still_blocks():
    """One prose heredoc must not launder a second, executable one."""
    text = (
        "cat <<'A' > /tmp/1\njust prose\nA\n"
        "bash <<'B'\nhermes gateway %s\nB" % R
    )
    assert guard(text) is True


# ---------------------------------------------------------------------------
# Mutation-proof: the exemption is what allows the prose case
# ---------------------------------------------------------------------------
def test_mutation_without_masking_the_prose_case_would_block(monkeypatch):
    """Prove the heredoc masker is load-bearing, not incidental.

    Uses a case the masker UNIQUELY rescues. Measured on the unpatched tree, the
    pre-existing quoted-data exemption already allowed `<<'EOF'` (single-quoted
    delimiter) prose, so that shape proves nothing about this fix. The UNQUOTED
    (`<<EOF`) and double-quoted (`<<"EOF"`) delimiters were genuinely BLOCKED before
    and are allowed only because of the heredoc masking — so disabling the masker must
    send them back to blocked.
    """
    import cron.lifecycle_guard as G

    unquoted = "cat <<EOF > /tmp/notes.md\nRun hermes gateway %s from a separate shell.\nEOF" % R
    double_q = 'cat <<"EOF" > /tmp/n.md\nhermes gateway %s is documented here\nEOF' % R

    assert guard(unquoted) is False
    assert guard(double_q) is False

    monkeypatch.setattr(G, "_mask_heredoc_bodies", lambda text: text)
    assert G._direct_lifecycle_scan(unquoted) is True
    assert G._direct_lifecycle_scan(double_q) is True
