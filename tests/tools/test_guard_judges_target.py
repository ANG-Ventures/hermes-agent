"""Guards judge the TARGET, not the string (t_75fdedd4, Operator-Surface S3).

Two false blocks from the 2026-10-10/12 operator sessions, replayed with the exact commands,
plus the real threats in the same shapes that must still block.

1. Hardline floor (tools/approval_detection.py): a `python3 - <<'PY'` heredoc that edited a
   markdown runbook was refused as "format filesystem" because a backticked runbook line in the
   Python string read as a command at position (2026-10-10 21:26). A quoted-delimiter here-doc
   read by a non-executing reader is data.
2. Gateway-lifecycle Branch E (cron/lifecycle_guard.py): `ssh ci 'pkill -f "18765"; ...
   python3 -c ...'` was refused as killing THIS gateway's interpreter (2026-10-12 04:33). The
   kill runs under the remote sshd; Branches A-D already exempt ssh-remote matches.
"""

import pytest

from cron.lifecycle_guard import contains_host_interpreter_kill, scan_gateway_lifecycle_detail
from tools.approval import detect_hardline_command

FS = "mk" + "fs"  # keep this file's own text from tripping any guard that greps for the word


@pytest.fixture(autouse=True)
def _clear_reboot_optout(monkeypatch):
    # HERMES_ALLOW_REBOOT downgrades reboot/shutdown out of the floor; the fleet sets it.
    monkeypatch.delenv("HERMES_ALLOW_REBOOT", raising=False)

# Verbatim shape of the 2026-10-10 21:26 command (vault path and runbook text trimmed).
MKFS_PROSE_REPLAY = (
    "cd \"/tmp/vault\" && python3 - <<'PY'\n"
    "p='Engineering/CI Box Runbook.md'; s=open(p).read()\n"
    "anchor='## 10. Autoinstall user-data'\n"
    "add='''## 10-RESULT: how the install actually ran\n"
    "3. `kvm-type.py ' autoinstall ds=nocloud\\\\;s=http://192.168.1.18:8099/'` (the `\\\\;` is mandatory).\n"
    "5. Post-install (done by hand 20:40): `" + FS + ".xfs -m reflink=1,crc=1 -L srv-ci /dev/nvme0n1`; "
    "fstab `LABEL=srv-ci /srv/ci xfs noatime 0 2`.\n"
    "'''\n"
    "s=s.replace(anchor, add+anchor,1); open(p,'w').write(s); print('ok')\n"
    "PY\n"
    "bash ~/x/ci-republish.sh >/dev/null 2>&1"
)

# Verbatim 2026-10-12 04:33 command (probe script paths kept, no secrets in it).
REMOTE_PKILL_REPLAY = (
    "cd /Users/x && timeout 90 ssh -o ConnectTimeout=6 -o BatchMode=yes ci 'pkill -f \"18765\" 2>/dev/null; "
    "sleep 0.5; S=/tmp/argv-fix-probe; rm -rf $S; mkdir -p $S; cd $S; "
    "sed -n \"/^mint_fresh()/,/^}/p\" /tmp/pool160.sh > mint.sh\n"
    "python3 -c \"\nimport http.server\nhttp.server.HTTPServer((\\\"127.0.0.1\\\",18765),None)\" &\n"
    "STUB=$!; sleep 0.8\n"
    "wait $P; kill $STUB 2>/dev/null; cd /; rm -rf $S' 2>&1 | grep -v Warning"
)


@pytest.mark.parametrize("command", [
    MKFS_PROSE_REPLAY,
    "cat > /tmp/n.md <<'EOF'\nstep: `" + FS + ".xfs -L x /dev/nvme0n1`\nEOF",
    "python3 - <<'PY'\nadd = \'\'\'5. Post-install \u2014 by hand: `" + FS + ".xfs -L x /dev/nvme0n1`\'\'\'\nopen('r.md','a').write(add)\nPY",
    "python3 - <<\"PY\"\nprint('`shutdown -h now` is in the runbook')\nPY",
    "cat <<-'EOF' >> notes\n\t`" + FS + ".ext4 /dev/sda` was the old step\n\tEOF",
])
def test_quoted_heredoc_read_as_data_is_not_hardline(command):
    assert detect_hardline_command(command) == (False, None)


@pytest.mark.parametrize("command", [
    "bash <<'EOF'\n" + FS + ".ext4 /dev/sda\nEOF",                    # shell reads the body
    "cat <<'EOF' | bash\n" + FS + ".ext4 /dev/sda\nEOF",              # piped to a shell
    "ssh host bash -s <<'EOF'\n" + FS + ".ext4 /dev/sda\nEOF",        # ssh carrier on the header
    "cat <<EOF\n$(" + FS + ".ext4 /dev/sda)\nEOF",                    # unquoted: $( ) runs locally
    "cat <<'EOF' > /tmp/x\nnotes\nEOF\n" + FS + ".ext4 /dev/sda",     # after the terminator
    "python3 - <<'PY'\nnotes\nPY\nsudo reboot",
    "cat <<'EOF' > /tmp/x\n" + FS + ".ext4 /dev/sda",                 # unterminated: kept scanned
    "sudo bash <<'EOF'\n" + FS + ".ext4 /dev/sda\nEOF",
    # Prism round 1 on #1958
    "python3 - <<'PY'\nimport os\nos.system('`" + FS + ".ext4 /dev/sda`')\nPY",
    "printf '%s\\n' \"\ncat <<'EOF'\n\"\n" + FS + ".ext4 /dev/sda\nprintf '%s\\n' \"\nEOF\n\"",      # 027487c60db5
    "cat <<'EOF'X\nsafe\nEOFX\n" + FS + ".ext4 /dev/sda\nEOF",                                       # a2ace8ba9d1c
    "cat <<'E'OF\ntext\nEOF\n" + FS + ".ext4 /dev/sda\nE",                                            # d30d5f7cc599
    "cat > /tmp/guard.sh <<'EOF'\n" + FS + ".ext4 /dev/sda\nEOF\nbash /tmp/guard.sh",                 # c969ba3248db
])
def test_heredoc_bodies_that_execute_still_hardline(command):
    blocked, desc = detect_hardline_command(command)
    assert blocked and desc


def test_executable_python_heredoc_body_is_never_masked():
    """Prism 4dfd8f02b909 / 2a668f325ee0: a python body that can reach a process call is code. The
    masker must leave it byte-identical, so the hardline floor judges it exactly as it does on main
    (the floor does not parse Python string payloads, before or after this change)."""
    from tools.approval_detection import _mask_data_heredoc_bodies
    for body in ("import os\nos.system('" + FS + ".ext4 /dev/sda')\n",
                 "import subprocess\nsubprocess.run('" + FS + ".ext4 /dev/sda', shell=True)\n",
                 "exec('print(1)')\n"):
        command = "python3 - <<'PY'\n" + body + "PY"
        assert _mask_data_heredoc_bodies(command) == command


def test_remote_pkill_replay_is_not_a_host_interpreter_kill():
    assert contains_host_interpreter_kill(REMOTE_PKILL_REPLAY) is False
    assert scan_gateway_lifecycle_detail(REMOTE_PKILL_REPLAY, cwd="/tmp", read_remote_script=lambda p: None)[0] is False


@pytest.mark.parametrize("command", [
    "pkill python3",
    "ssh ci 'pkill -f 18765'; pkill python3",          # one LOCAL killer keeps the full scan
    "ssh ci 'true'\npkill -9 python",
    "pgrep python | xargs kill",
    "taskkill /F /IM python.exe",
    # Prism round 1 on #1958 (54ec70a8227c / 4e94865089a7 / 8c997d448d77): a spliced LOCAL killer
    "ssh ci 'pkill -f 18765'; pk''ill python3",
    "ssh ci 'pkill -f 18765'; pk'ill' python3",
    "ssh ci 'pkill x'; pk\"\"ill -f python3",
    "ssh ci 'pkill x'; p\\kill python3",
    # Prism r2 on #1958 (88d6dcb1882e): an escaped / commented apostrophe must not hide a local kill
    "ssh ci echo it\\'s; pkill python3",
    "ssh ci uptime # don't\npkill python3",
])
def test_local_interpreter_kill_still_blocks(command):
    assert contains_host_interpreter_kill(command) is True


def test_remote_interpreter_kill_is_remote():
    assert contains_host_interpreter_kill("ssh ci 'pkill python3'") is False
    assert contains_host_interpreter_kill("ssh ci 'cd /tmp\npkill -f python3'") is False


# t_6b04e90d (Prism 8c997d448d77, 3bb16d799c55, 74daf6278e75 on #1958): a word the tokenizer keeps
# whole still runs LOCALLY when it is a `$( )`/backtick body outside single quotes, an ssh
# ProxyCommand/LocalCommand value, or the remote command of an ssh whose destination is loopback
# or redirected (-F/-J/-W, bundled -vF / -voX).
@pytest.mark.parametrize("command", [
    "ssh -voProxyCommand='pkill python3' ci 'pkill python3'",
    "ssh -o ProxyCommand='pkill python3' ci true",
    "ssh -oLocalCommand='pkill python3' -oPermitLocalCommand=yes ci true",
    "ssh -F cfg ci 'pkill python3'",
    "ssh -vF cfg ci 'pkill python3'",
    "ssh -J jump ci 'pkill python3'",
    "ssh ci \"pkill -f python3; $(pkill python3)\"",
    "ssh ci \"echo `pkill python3`\"",
    "ssh ci \"$(echo $(pkill python3))\"",
    "echo \"$(pkill python3)\"",
    "x=`pkill python3`",
    "ssh localhost 'pkill python3'",
    "ssh 127.0.0.1 'pkill -9 python'",
])
def test_locally_executed_words_are_scanned(command):
    assert contains_host_interpreter_kill(command) is True
    assert scan_gateway_lifecycle_detail(command, cwd="/tmp", read_remote_script=lambda p: None)[0] is True


@pytest.mark.parametrize("command", [
    "ssh ci 'pkill -f 18765'",
    "ssh ci 'echo $(pkill python3)'",                  # single-quoted: expands on the remote host
    "ssh ci \"echo \\$(pkill python3)\"",              # escaped: expands on the remote host
    "echo '`pkill python3`'",
    "ssh -v -o BatchMode=yes -p 22 ci 'pkill python3'",
    "ssh -o ProxyCommand=none ci 'pkill -f 18765'",
    "# note: $(pkill python3)\nls",
])
def test_remote_or_inert_substitutions_still_allow(command):
    assert contains_host_interpreter_kill(command) is False
