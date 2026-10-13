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
    "git commit -F - <<'EOF'\nran `" + FS + ".xfs` by hand, then `sudo reboot`\nEOF",
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
])
def test_heredoc_bodies_that_execute_still_hardline(command):
    blocked, desc = detect_hardline_command(command)
    assert blocked and desc


def test_remote_pkill_replay_is_not_a_host_interpreter_kill():
    assert contains_host_interpreter_kill(REMOTE_PKILL_REPLAY) is False
    assert scan_gateway_lifecycle_detail(REMOTE_PKILL_REPLAY, cwd="/tmp", read_remote_script=lambda p: None)[0] is False


@pytest.mark.parametrize("command", [
    "pkill python3",
    "ssh ci 'pkill -f 18765'; pkill python3",          # one LOCAL killer keeps the full scan
    "ssh ci 'true'\npkill -9 python",
    "pgrep python | xargs kill",
    "taskkill /F /IM python.exe",
])
def test_local_interpreter_kill_still_blocks(command):
    assert contains_host_interpreter_kill(command) is True


def test_remote_interpreter_kill_is_remote():
    assert contains_host_interpreter_kill("ssh ci 'pkill python3'") is False
    assert contains_host_interpreter_kill("ssh ci 'cd /tmp\npkill -f python3'") is False
