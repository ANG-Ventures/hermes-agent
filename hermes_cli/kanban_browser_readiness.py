"""Fresh, read-only browserd-host admission. Cache only within one dispatch tick."""
import json
import subprocess
import sys


def probe(home, host):
    script = home / 'scripts' / 'browsers' / 'dispatch_relay.py'
    try:
        out = subprocess.run([sys.executable, str(script), 'readiness', '--host', host],
                             capture_output=True, text=True, timeout=25, stdin=subprocess.DEVNULL)
        resp = json.loads(((out.stdout or '').strip().splitlines() or ['{}'])[-1])
        if not isinstance(resp, dict):
            raise ValueError('invalid reply')
        if out.returncode == 0 and resp.get('ok') is True and resp.get('host') == host:
            return resp
        return {'ok': False, 'host': host, 'refusal': resp.get('refusal') or 'BROWSER_READINESS_UNAVAILABLE',
                'detail': str(resp.get('detail') or 'invalid readiness reply')[:300]}
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return {'ok': False, 'host': host, 'refusal': 'BROWSER_READINESS_UNAVAILABLE', 'detail': type(exc).__name__}
