#!/bin/bash
# Hermetic stand-in for ssh (t_db46f06b identity probe tests): runs the remote command locally as the "remote" user
# would see it: HOME=$FAKE_SSH_HOME, a clean env, plus only the SendEnv names (sshd AcceptEnv) with the CLIENT's
# values. Never touches the network.
# Like OpenSSH, `-E <log>` whose file cannot be opened fails BEFORE the remote command runs (rc 1). `-O <cmd>`
# appends the ControlPath it was aimed at to $FAKE_SSH_CONTROL_LOG (when set) and exits 0.
send=()
control_path=""
ctl=""
while [ $# -gt 0 ]; do
  case "$1" in
    -O) ctl="$2"; shift 2 ;;
    -o) case "$2" in
          SendEnv=*) send+=("${2#SendEnv=}") ;;
          ControlPath=*) control_path="${2#ControlPath=}" ;;
        esac; shift 2 ;;
    -E) if ! : >>"$2" 2>/dev/null; then echo "Couldn't open logfile $2: No such file or directory" >&2; exit 1; fi
        shift 2 ;;
    -p|-i|-l|-F) shift 2 ;;
    -*) shift ;;
    *) shift; break ;;                              # user@host; the rest is the remote command
  esac
done
if [ -n "$ctl" ]; then
  [ -n "$FAKE_SSH_CONTROL_LOG" ] && echo "$ctl $control_path" >>"$FAKE_SSH_CONTROL_LOG"
  exit 0
fi
envs=(HOME="$FAKE_SSH_HOME" PATH=/usr/bin:/bin GIT_CONFIG_NOSYSTEM=1 USER=kanbanw)
for n in "${send[@]}"; do
  [ -n "${!n+x}" ] && envs+=("$n=${!n}")
done
cd "$FAKE_SSH_HOME" || exit 255
exec env -i "${envs[@]}" bash -c "$*"
