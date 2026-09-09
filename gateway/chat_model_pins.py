"""Credential-free, cross-process model preferences keyed by destination chat.

Separate from transcript routing so key migrations, resets, and participant
isolation cannot erase a user's choice. NULL is an explicit clear tombstone:
legacy session snapshots must never resurrect a cleared pin.
"""
from contextlib import closing
import json
from pathlib import Path
import sqlite3


class ChatModelPins:
    def __init__(self, sessions_dir: Path):
        self.path = Path(sessions_dir) / "chat-model-pins.sqlite3"

    def get(self, namespace: str, platform: str, chat_id: str):
        if not self.path.exists():
            return False, None
        with closing(sqlite3.connect(self.path, timeout=10)) as conn:
            row = conn.execute(
                "SELECT identity FROM chat_model_pins WHERE namespace=? AND platform=? AND chat_id=?",
                (namespace, platform, str(chat_id)),
            ).fetchone()
        if row is None:
            return False, None
        if row[0] is None:
            return True, None
        from gateway.session import sanitize_model_override_identity
        identity = sanitize_model_override_identity(json.loads(row[0]))
        if identity is None:
            raise ValueError("Invalid persisted chat model pin")
        return True, identity

    def set(self, namespace: str, platform: str, chat_id: str, identity, *, seed=False):
        from gateway.session import sanitize_model_override_identity

        clean = sanitize_model_override_identity(identity)
        if identity is not None and clean is None:
            raise ValueError("Chat model pin requires model and provider")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=10)) as conn, conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS chat_model_pins ("
                "namespace TEXT NOT NULL, platform TEXT NOT NULL, chat_id TEXT NOT NULL, "
                "identity TEXT, PRIMARY KEY(namespace, platform, chat_id))"
            )
            sql = (
                "INSERT OR IGNORE INTO chat_model_pins VALUES (?, ?, ?, ?)" if seed else
                "INSERT INTO chat_model_pins VALUES (?, ?, ?, ?) ON CONFLICT(namespace, platform, chat_id) "
                "DO UPDATE SET identity=excluded.identity"
            )
            conn.execute(sql, (namespace, platform, str(chat_id), json.dumps(clean) if clean else None))
