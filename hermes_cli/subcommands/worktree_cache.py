"""``hermes worktree-cache`` subcommand parser (spec: hermes-content plans/fleet-worktree-cache)."""

from __future__ import annotations


def build_worktree_cache_parser(subparsers) -> None:
    """Attach the ``worktree-cache`` subcommand to ``subparsers``."""
    parser = subparsers.add_parser(
        "worktree-cache", help="Copy-on-write checkouts from a per-host template (default off)",
        description="Clone fleet repos by copy-on-write cloning a per-host template checkout "
            "(worktree_cache.* in config.yaml; off unless enabled and the caller's scope is listed). "
            "Every fallback to plain git is counted in ~/.hermes/state/clone-fallback.jsonl.")
    from hermes_cli import worktree_cache as _wc
    _wc.add_arguments(parser)

    def _dispatch(args):  # noqa: ANN001
        if getattr(args, "worktree_cache_action", None) is None:
            parser.print_help()
            return 0
        return args.func(args)

    parser.set_defaults(func=_dispatch)
