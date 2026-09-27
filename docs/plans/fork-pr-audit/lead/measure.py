"""Lead's measurement classifier for KEEP-UNPROVEN rows (recorded in FINAL.md)."""
import re

DATE = re.compile(r'\d{4}-\d{2}-\d{2}(T[\d:]+Z?)?|\d{2}-\d{2}|\d{1,2}:\d{2}')
NOISE = re.compile(r'\b\d[\d,]*\s*(rotated|files?|logs?|profiles?|agent/gateway|gateway/agent|days?|d\b|h\b|mins?|subs|literals?|configs?|sessions? ago)', re.I)
WINDOW = re.compile(r'\[?window[^\]]*\]?|\((?:[^()]*mtime[^()]*)\)|logs?: [^;|]*', re.I)
ZERO = re.compile(r'(^|[\s(\'"=:])0\s', re.I)


def positive_fires(f):
    if not f:
        return 0
    s = str(f)
    s = WINDOW.sub(' ', s)
    s = DATE.sub(' ', s)
    s = NOISE.sub(' ', s)
    s = re.sub(r'\b(py)?3\.\d+|#\d+|t_[0-9a-f]{8}|[0-9a-f]{10,}|lsp\.max_servers_per_host=N', ' ', s)
    nums = [int(n.replace(',', '')) for n in re.findall(r'(?<![\w.])(\d[\d,]*)(?![\w.%])', s)]
    return max(nums) if nums else 0


def measured(x, verdicts_by_key=None):
    """Return (bool, reason) — the lead's measurement for a KEEP-UNPROVEN row."""
    e = x.get('evidence') or {}
    adv = x.get('adversary') or {}
    f = e.get('fires')
    us = str(e.get('upstream_state') or '')
    txt = ' '.join(str(v) for v in [x.get('notes'), adv.get('evidence'), adv.get('notes'), e.get('original'), f])
    n = positive_fires(f)
    if n > 0:
        return True, f'fires>0 ({n}) in evidence.fires: {str(f)[:140]}'
    if re.search(r'\bRED\b', us) and not re.search(r'read-only|by read|\(read|read:', us, re.I) and re.search(r'fail|pytest|test|probe|ran', us, re.I):
        return True, f'upstream RED by executed test/probe: {us[:140]}'
    if re.search(r'fork-permanent', txt, re.I):
        return True, 'fork-features.json fork-permanent entry cited'
    if re.search(r'config\.yaml:\d+|set in [^;]*config|jobs\.json|\d+ (live )?(jobs|profiles|configs)', txt, re.I):
        return True, 'live fleet config/knob cited: ' + (re.search(r'config\.yaml:\d+|set in [^;]*config|jobs\.json|\d+ (live )?(jobs|profiles|configs)', txt, re.I).group(0))
    deps = ((e.get('cost') or {}).get('dependents')) or []
    if verdicts_by_key is not None:
        kd = [d for d in deps if (verdicts_by_key.get(d) or '').startswith('KEEP')]
        if kd:
            return True, 'KEEP-by-dependency: KEEP rows build on it: ' + ', '.join(kd[:6])
    return False, 'no positive fire count, no executed upstream RED, no live knob/consumer/registry entry found in the row evidence'
