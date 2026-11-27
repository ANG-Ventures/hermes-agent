"""Direct bounded fixture probes when test-gate admission is unavailable."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest


def main():
    failures = 0
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        os.environ['HERMES_HOME'] = str(root / 'home')
        os.environ['HOME'] = str(root / 'user')
        if len(sys.argv) > 1:
            old_spec = importlib.util.spec_from_file_location('tools.spawn_nofork', sys.argv[1])
            old = importlib.util.module_from_spec(old_spec)
            sys.modules['tools.spawn_nofork'] = old
            old_spec.loader.exec_module(old)
        spec = importlib.util.spec_from_file_location('spawn_cases', 'tests/tools/test_spawn_nofork.py')
        cases = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cases)
        for name, fn in list(vars(cases).items()):
            if not name.startswith('test_'):
                continue
            case = root / name
            case.mkdir()
            mp = pytest.MonkeyPatch()
            environment = None
            try:
                params = {}
                for param in __import__('inspect').signature(fn).parameters:
                    if param == 'tmp_path':
                        params[param] = case
                    elif param == 'monkeypatch':
                        params[param] = mp
                    elif param == 'no_fork_exec':
                        cases.no_fork_exec.__wrapped__(mp)
                        params[param] = None
                    elif param == 'env':
                        work = case / 'work dir'
                        work.mkdir()
                        environment = cases.LocalEnvironment(cwd=str(work), timeout=30)
                        params[param] = environment
                fn(**params)
                print('PASS', name, flush=True)
            except BaseException:
                failures += 1
                print('FAIL', name, flush=True)
                __import__('traceback').print_exc()
            finally:
                if environment:
                    environment.cleanup()
                mp.undo()
    return bool(failures)


if __name__ == '__main__':
    raise SystemExit(main())
