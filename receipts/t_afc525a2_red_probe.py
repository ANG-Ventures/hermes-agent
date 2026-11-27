"""Regression reproductions against the original PR helper, without mutating source."""
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile


def main():
    source = Path('receipts/t_afc525a2_old_spawn.py').read_text(encoding='utf-8')
    real_popen = subprocess.Popen
    subprocess.Popen = lambda *a, **k: real_popen(*a, **k)
    try:
        try:
            exec(compile(source, 'old_spawn', 'exec'), {'__name__': 'old_spawn'})
        except TypeError as exc:
            print('RED import with Popen function:', exc)
        else:
            raise AssertionError('import unexpectedly worked')
    finally:
        subprocess.Popen = real_popen
    spec = importlib.util.spec_from_file_location('old_spawn', 'receipts/t_afc525a2_old_spawn.py')
    old = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(old)
    with tempfile.TemporaryDirectory() as d:
        fd = os.open(Path(d) / 'secret', os.O_CREAT | os.O_RDWR, 0o600)
        real_spawn = os.posix_spawn

        def race(*a, **k):
            os.set_inheritable(fd, True)
            return real_spawn(*a, **k)

        os.posix_spawn = race
        try:
            proc = old.spawn_popen(['/bin/sh', '-c', f'[ -e /dev/fd/{fd} ] && echo leaked || echo closed'],
                                   stdout=subprocess.PIPE, text=True)
            out = proc.communicate(timeout=10)[0].strip()
            assert out == 'leaked', out
            print('RED late inheritable descriptor:', out)
        finally:
            os.posix_spawn = real_spawn
            os.close(fd)
        result = old.run_nofork(['/bin/cat'], input='piped', stdout=subprocess.PIPE, text=True)
        assert result.stdout != 'piped'
        print('RED run input:', repr(result.stdout))


if __name__ == '__main__':
    main()
