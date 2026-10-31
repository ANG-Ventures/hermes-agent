"""Dashboard TestClients that name the board on every request, as the SPA does (S7 spec §6).

Since S7 an omitted ``?board=`` is refused (400 ``board required``) under the shipped
``kanban.bare_verb: refuse``. Tests that exercise a route's behaviour, not board selection,
wrap their client with :func:`naming_board`. A request whose URL already carries ``board``
keeps it. Tests of the omitted-board refusal keep a bare client.
"""
from __future__ import annotations

import httpx


def naming_board(client: httpx.Client, board: str = "default") -> httpx.Client:
    def _add_board(request: httpx.Request) -> None:
        if "board" not in request.url.params:
            request.url = request.url.copy_add_param("board", board)

    client.event_hooks = {**client.event_hooks, "request": [*client.event_hooks.get("request", []), _add_board]}
    return client
