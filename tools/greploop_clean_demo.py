"""Parse a retry-after duration string into seconds. (greploop ARM-proof demo)"""
from typing import Optional

_UNIT_SECONDS = {"h": 3600.0, "m": 60.0, "s": 1.0}


def parse_retry_after(value: object) -> Optional[float]:
    """Return the duration in seconds, or None if the value cannot be parsed.

    None is returned (never 0) on unparseable input so callers can distinguish a
    parse failure from a legitimate zero-second delay.
    """
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    unit = _UNIT_SECONDS.get(text[-1])
    number = text[:-1] if unit is not None else text
    try:
        seconds = float(number) * (unit if unit is not None else 1.0)
    except (ValueError, TypeError):
        return None
    return seconds if seconds >= 0 else None
