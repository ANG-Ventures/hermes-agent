"""Staging DISK IMAGES must not ride into the backup.

2026-08-09: ``var/subvps-staging.sparseimage`` — a 42 GB case-sensitive APFS sparse
image that ``sub-vps-backup-pull.py`` auto-creates as rsync staging scratch — was
being archived into every full-tier backup. It drove the Sunday bundle from
~15.8 GB to ~30 GB per agent (56 GB on the wire, both Apollo and Aegis) which at
the measured ~9.7 Mbit/s upstream is ~13.8h of upload, and wedged the offsite lane
for a day while the heartbeat deadman paged every 30 minutes.

The image is a CONTAINER for data whose real home is the sub-VPS boxes (backed up
separately by restic). Archiving it duplicates that data and dwarfs the agent state
the backup exists to protect.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from hermes_cli.backup import _should_exclude  # noqa: E402


class TestDiskImagesExcluded:
    def test_the_actual_offender_is_excluded(self):
        assert _should_exclude(Path("var/subvps-staging.sparseimage")) is True

    def test_disk_image_suffixes_excluded_anywhere(self):
        for rel in (
            "var/subvps-staging.sparseimage",
            "state/staging/scratch.sparsebundle",
            "tmp/installer.dmg",
            "deep/nested/path/thing.sparseimage",
        ):
            assert _should_exclude(Path(rel)) is True, rel


class TestNoOverReach:
    """The suffix must not swallow legitimate agent state.

    Without these the rule could be 'exclude anything with the word sparseimage',
    which would drop real notes/scripts about the staging lane.
    """

    def test_docs_and_scripts_about_disk_images_are_kept(self):
        for rel in (
            "skills/devops/fleet-backup/references/sub-vps-backup-lane.md",
            "scripts/sub-vps-backup-pull.py",
            "notes/sparseimage-runbook.md",       # name mentions it, not a .sparseimage
            "state/sparseimage-notes.txt",
        ):
            assert _should_exclude(Path(rel)) is False, rel

    def test_a_dir_named_like_the_suffix_is_kept(self):
        # a DIRECTORY component ending in .dmg must not drop files beneath it
        assert _should_exclude(Path("projects/build.dmg.d/manifest.json")) is False

    def test_ordinary_agent_state_still_backed_up(self):
        for rel in (
            "state/backup/last-success-backup-drive",   # tiny heartbeat, must stay
            "memories/MEMORY.md",
            "cron/jobs.json",
        ):
            assert _should_exclude(Path(rel)) is False, rel
