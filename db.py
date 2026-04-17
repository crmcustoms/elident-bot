import sqlite3
from config import settings


class ConversationDB:
    def __init__(self):
        self.path = settings.db_file
        self._init()

    def _init(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_id INTEGER NOT NULL,
                    role      TEXT NOT NULL,
                    content   TEXT NOT NULL,
                    ts        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_entity ON messages(entity_id)"
            )

    def get_history(self, entity_id: int, limit: int = 20) -> list[dict]:
        with sqlite3.connect(self.path) as conn:
            rows = conn.execute(
                "SELECT role, content FROM messages "
                "WHERE entity_id=? ORDER BY id DESC LIMIT ?",
                (entity_id, limit),
            ).fetchall()
        return [{"role": r, "content": c} for r, c in reversed(rows)]

    def add(self, entity_id: int, role: str, content: str):
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "INSERT INTO messages (entity_id, role, content) VALUES (?,?,?)",
                (entity_id, role, content),
            )
