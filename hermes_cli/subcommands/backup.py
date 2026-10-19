"""``hermes backup`` subcommand parser."""

from __future__ import annotations

from typing import Callable


def build_backup_parser(subparsers, *, cmd_backup: Callable) -> None:
    """Attach the ``backup`` subcommand to ``subparsers``."""
    backup_parser = subparsers.add_parser(
        "backup", help="Back up Hermes home directory to a zip file",
        description="Create a zip archive of your entire Hermes configuration, "
        "skills, sessions, and data (excludes the hermes-agent codebase). "
        "Use --quick for a fast snapshot of just critical state files.")
    backup_parser.add_argument(
        "-o",
        "--output", help="Output path for the zip file (default: ~/hermes-backup-<timestamp>.zip)")
    backup_parser.add_argument(
        "-q", "--quick", action="store_true",
        help="Quick snapshot: only critical state files (config, state.db, .env, auth, cron)")
    backup_parser.add_argument(
        "-l", "--label", help="Label for the snapshot (only used with --quick)")
    backup_parser.add_argument(
        "-k", "--keep", type=int, default=3, metavar="N",
        help="After a full backup, delete older hermes-backup-*.zip files in the output "
             "directory beyond the newest N (default 3; 0 keeps everything)")
    backup_parser.add_argument(
        "--census-out", metavar="PATH",
        help="Full backup only: after the scan, write the would-carry census (files, bytes, per-dir) as JSON here")
    backup_parser.add_argument(
        "--census-gate", metavar="CMD",
        help="Full backup only (needs --census-out): run CMD (no shell) after the census and before the zip "
             "opens; a nonzero exit aborts with that exit status (75 = refused) and writes no archive")
    backup_parser.set_defaults(func=cmd_backup)
