"""Config-backed scratch placement and fail-closed mount admission.

This is admission, not recovery: a vanished persisted workspace needs operator
recovery rather than creation of an empty replacement.
"""
from pathlib import Path
import os


class WorkspaceUnavailable(ValueError):
    """Workspace admission refused without charging a worker failure."""


def configured_root():
    from hermes_cli.config import load_config_readonly

    config = load_config_readonly().get("kanban", {})
    raw = config.get("workspaces_root")
    required = config.get("workspaces_root_require_mount", False)
    if not isinstance(required, bool):
        raise WorkspaceUnavailable("workspaces_root_invalid: require_mount must be boolean")
    if raw in (None, ""):
        if required:
            raise WorkspaceUnavailable("workspaces_root_invalid: mount guard requires a root")
        return None, False
    if not isinstance(raw, str) or not Path(raw).expanduser().is_absolute():
        raise WorkspaceUnavailable("workspaces_root_invalid: root must be absolute")
    return Path(raw).expanduser(), required


def validate_mount(root):
    # Permit a pre-provisioned directory directly below a mount, as well as
    # the mount itself. Never walk upwards to / and call the SSD a valid mount.
    if not root.is_dir() or root.is_symlink():
        raise WorkspaceUnavailable(f"workspaces_root_unmounted: {root}")
    if not (os.path.ismount(root) or os.path.ismount(root.parent)):
        raise WorkspaceUnavailable(f"workspaces_root_unmounted: {root}")
    if root.resolve() != root.absolute():
        raise WorkspaceUnavailable(f"workspaces_root_invalid: symlink ancestor: {root}")


def validate_persisted(path):
    if not path.is_dir():
        raise WorkspaceUnavailable(f"workspace_missing: stranded_by_mount_loss: {path}")
