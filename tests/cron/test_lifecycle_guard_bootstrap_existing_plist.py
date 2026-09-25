"""``launchctl bootstrap`` of an EXISTING non-gateway plist is allowed.

submit/bootstrap are refused label-independently because a NEW job's label is chosen by whoever
writes the command. A plist already on disk is different: launchd reads ``Label`` from the file,
so reloading an unrelated daemon after editing its plist is provably not a gateway operation.
"""
import os
import plistlib

import pytest

from cron.lifecycle_guard import contains_gateway_lifecycle_command_or_referenced_script as blocked

GW = "ai.hermes.gateway"


def _plist(tmp_path, name, label, program=("/bin/true",)):
    path = tmp_path / name
    body = {"ProgramArguments": list(program)}
    if label is not None:
        body["Label"] = label
    with open(path, "wb") as fh:
        plistlib.dump(body, fh)
    return path


def test_existing_non_gateway_plist_is_allowed(tmp_path):
    p = _plist(tmp_path, "com.example.router.plist", "com.example.router")
    assert not blocked(f"launchctl bootstrap gui/501 {p}")
    assert not blocked(f"sudo launchctl bootstrap system {p}")


@pytest.mark.parametrize("make", [
    lambda t: f"{_plist(t, 'gw.plist', GW)}",                                   # gateway label
    lambda t: f"{_plist(t, 'x.plist', GW + '-aegis')}",
    lambda t: f"{_plist(t, 'h.plist', 'com.x.helper', ('/bin/sh', '-c', 'hermes gateway restart'))}",
    lambda t: f"{_plist(t, 'n.plist', None)}",                                   # no Label
    lambda t: f"{t}/missing.plist",
    lambda t: (os.mkdir(t / 'd.plist'), f"{t}/d.plist")[1],                       # directory
    lambda t: (open(t / 'bad.plist', 'w').write('not a plist'), f"{t}/bad.plist")[1],
    lambda t: "$PLIST",
    lambda t: "",                                                                 # no plist arg
    lambda t: f"{_plist(t, 'ok.plist', 'com.x.ok')} {_plist(t, 'gw2.plist', GW)}",  # mixed
])
def test_everything_not_provably_non_gateway_stays_blocked(tmp_path, make):
    assert blocked(f"launchctl bootstrap gui/501 {make(tmp_path)}".rstrip())


def test_oversized_plist_stays_blocked(tmp_path):
    p = _plist(tmp_path, "big.plist", "com.x.big", ("/bin/true", "x" * (300 * 1024)))
    assert blocked(f"launchctl bootstrap gui/501 {p}")


def test_submit_stays_blocked_in_every_shape():
    assert blocked("launchctl submit -l com.x.anything -- /bin/true")
