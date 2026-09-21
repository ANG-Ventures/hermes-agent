"""Disposable proof of the task's proposed version-bump premise."""
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
    original = {'version': 2, 'attempts': [], 'session_attempts': {'fixture-session': {'count': 3, 'attempted_at': 1000.0}}}
    path.write_text(json.dumps(original))
    store = old.AutoResumeAttemptStore(path, now=lambda: 1000.0)
    print('old_writer_commit=cea2ef75e0 old_version=', old._STORE_VERSION)
    print('before=', original)
    print('old_session_resume_verdict=', store.session_resume_verdict('fixture-session', 3))
    after = json.loads(path.read_text())
    print('after=', after)
    assert after == original, 'Version bump does NOT protect against the shipped v1 repair writer'
