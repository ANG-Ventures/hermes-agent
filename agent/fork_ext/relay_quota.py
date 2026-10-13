"""Remote relay quota evidence for the main turn's existing exhaustion path."""
from agent.error_classifier import FailoverReason
from agent.fallback_events import relay_error_class
from agent.fork_ext.relay_headers import _POOL_CAPABILITY_PROVIDERS
from agent.retry_utils import RETRY_AFTER_CAP_RATE_LIMIT_S, parse_retry_after_seconds


def relay_quota_outlasts_retry_window(agent, error, classified) -> bool:
    """A structured quota on a known pool relay cannot recover within our wait policy.

    Local relay credentials are not remote seats. Do not rotate them or shorten
    the server's wait and retry the exhausted route. The caller exhausts this
    attempt block instead, preserving the native fallback walk and terminal error.
    Classification wins over quota-looking text/headers (notably deploy drain).
    """
    if (getattr(agent, "provider", "") or "").strip().lower() not in _POOL_CAPABILITY_PROVIDERS:
        return False
    if classified.reason not in (FailoverReason.rate_limit, FailoverReason.pool_exhausted):
        return False
    headers = getattr(getattr(error, "response", None), "headers", None)
    retry_after = parse_retry_after_seconds(headers)
    if retry_after is None or not retry_after > RETRY_AFTER_CAP_RATE_LIMIT_S:
        return False
    relay_class, _ = relay_error_class(error)
    if relay_class in ("quota_seat", "quota_model"):
        return True
    # Legacy passthrough 429s retain Anthropic's actual quota-window headers.
    # A generic 429, long Retry-After, or "quota" substring alone proves nothing.
    # An explicit non-quota relay class takes precedence over upstream evidence.
    if relay_class not in (None, "upstream_passthrough") or getattr(error, "status_code", None) != 429:
        return False
    return any(
        str(name).lower().startswith("anthropic-ratelimit-unified-")
        and str(name).lower().endswith("-status")
        and str(value).strip().lower() == "rejected"
        for name, value in (headers.items() if headers is not None else ())
    )
