"""Canonical dashboard link for gateway Kanban replies."""

from urllib.parse import quote


def dashboard_link(session_id: str | None) -> str | None:
    """Use the configured browser-facing origin, never guess a gateway host."""
    from hermes_cli.dashboard_auth.prefix import resolve_public_url

    public_url = resolve_public_url()
    if not public_url:
        return None
    url = public_url.rstrip("/") + "/kanban"
    return url + "?session=" + quote(session_id, safe="") if session_id else url
