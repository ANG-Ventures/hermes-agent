"""Small helper for parsing retry-after style duration strings. (greploop e2e v2)"""
from typing import Optional


def parse_retry_after(value) -> Optional[float]:
    """Parse a retry-after value (seconds number, or 'N s/m/h' string) into seconds.

    Returns the duration in seconds, or None if the value can't be parsed (callers must
    distinguish an unparseable input from a legitimate 0 — never silently treat failure as
    "no delay").
    """
    if value is None:
        return None
    s = str(value).strip().lower()
    if not s:
        return None
    try:
        if s.endswith("h"):
            return float(s[:-1]) * 3600
        if s.endswith("m"):
            return float(s[:-1]) * 60
        if s.endswith("s"):
            return float(s[:-1])
        return float(s)
    except (ValueError, TypeError):
        return None
