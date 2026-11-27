"""OS-account anchors shared by state and board guards; no database/auth imports."""
import os
from pathlib import Path


def account_home():
    try:
        import pwd
        home = pwd.getpwuid(os.getuid()).pw_dir.strip()
        if home and os.path.isdir(home):
            return Path(home)
    except Exception:
        pass
    return None


def deployed_root(account):
    declared = os.environ.get("HERMES_HOME", "").strip()
    if not declared or account is None:
        return None
    try:
        root = Path(declared).expanduser().resolve()
        account = account.resolve()
    except Exception:
        return None
    if root.parent.name == "profiles":
        root = root.parent.parent
    return root if root == account else None
