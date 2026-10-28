#!/bin/bash
# Hermetic stand-in for ssh (t_db46f06b identity probe tests): runs the remote command locally as the "remote" user
# would see it: HOME=$FAKE_SSH_HOME, a clean env, plus only the SendEnv names (sshd AcceptEnv) with the CLIENT's
# values. Never touches the network.
send=()
while [ $# -gt 0 ]; do
  case "$1" in
    -O) exit 0 ;;                                   # control command (-O exit): nothing to close
    -o) case "$2" in SendEnv=*) send+=("${2#SendEnv=}") ;; esac; shift 2 ;;
    -p|-i|-E|-l|-F) shift 2 ;;
    -*) shift ;;
    *) shift; break ;;                              # user@host; the rest is the remote command
  esac
done
envs=(HOME="$FAKE_SSH_HOME" PATH=/usr/bin:/bin GIT_CONFIG_NOSYSTEM=1 USER=kanbanw)
for n in "${send[@]}"; do
  [ -n "${!n+x}" ] && envs+=("$n=${!n}")
done
cd "$FAKE_SSH_HOME" || exit 255
exec env -i "${envs[@]}" bash -c "$*"
