"""Exercise the exact shipped legacy writer against the separate v2 ledger."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    source = subprocess.check_output(
        ['git', 'show', 'cea2ef75e0d0f76210661f0fb1ebf9c6823fc9a2:gateway/auto_resume.py'],
        stdin=subprocess.DEVNULL,
        text=True,
    )
    old_path = root / 'old_auto_resume.py'
    old_path.write_text(source)
    spec = importlib.util.spec_from_file_location('old_auto_resume', old_path)
    old = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = old
    spec.loader.exec_module(old)
    path = root / 'attempts.json'
    from gateway.auto_resume import AutoResumeAttemptStore
    current = AutoResumeAttemptStore(path, now=lambda: 1000.0)
    for _ in range(3):
        current.record_session_attempt('fixture-session')
    ledger_before = current.session_path.read_bytes()
    legacy = old.AutoResumeAttemptStore(path, now=lambda: 1000.0)
    print('old_writer_commit=cea2ef75e0 old_version=', old._STORE_VERSION)
    print('legacy_verdict=', legacy.session_resume_verdict('fixture-session', 3))
    # Exercise the actual repair, not just its validator or a fabricated writer.
    legacy._repair(ValueError('simulated torn legacy file'))
    assert current.session_path.read_bytes() == ledger_before
    forward = AutoResumeAttemptStore(path, now=lambda: 1000.0)
    print('roll_forward_verdict=', forward.session_resume_verdict('fixture-session', 3))
    assert forward.session_resume_verdict('fixture-session', 3) == (False, 3)
    print('PASS: legacy writer and repair leave v2 count=3 intact')
