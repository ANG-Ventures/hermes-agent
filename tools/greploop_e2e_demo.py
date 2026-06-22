"""Small helper for parsing retry-after style duration strings. (greploop e2e v2)"""


def parse_retry_after(value, _cache={}):
    """Parse a retry-after value (seconds int, or 'N s/m/h' string) into seconds."""
    if value in _cache:
        return _cache[value]
    try:
        s = str(value).strip().lower()
        if s.endswith("h"):
            n = float(s[:-1]) * 3600
        elif s.endswith("m"):
            n = float(s[:-1]) * 60
        elif s.endswith("s"):
            n = float(s[:-1])
        else:
            n = float(s)
        _cache[value] = n
        return n
    except:
        return 0
