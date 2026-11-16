"""Pure classification shared by CLI startup and database dispatch."""

READ_ACTIONS = frozenset({
    "show", "list", "ls", "allocation", "placement", "context", "runs", "attachments", "stats",
    "log", "tail", "locate", "notify-list", "notify-status", "placement-holds",
})


def is_read(args):
    return getattr(args, "kanban_action", None) in READ_ACTIONS
