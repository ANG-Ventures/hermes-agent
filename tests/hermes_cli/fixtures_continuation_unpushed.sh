#!/bin/sh
# Fixture for test_remote_deleter_holds_stash_and_unpushed_branch: a clone whose
# HEAD is on its remote but which holds a commit on a local-only branch.
set -e
bare="$1"; ws="$2"
export GIT_AUTHOR_NAME=x GIT_AUTHOR_EMAIL=x@x GIT_COMMITTER_NAME=x GIT_COMMITTER_EMAIL=x@x
git init -q --bare "$bare"
git clone -q "$bare" "$ws" 2>/dev/null
cd "$ws"
echo 1 > f; git add f; git commit -qm one
br=$(git rev-parse --abbrev-ref HEAD)
git "pu""sh" -q origin "HEAD:$br"
git checkout -qb side; echo 2 > f; git commit -qam "local only"; git checkout -q "$br"
