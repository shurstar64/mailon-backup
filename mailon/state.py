"""SQLite-backed state store for incremental sync.

Tracks:
  - messages: which mails we've already saved as Markdown
  - attachments: which attachments we've downloaded (or failed)
  - runs: execution history

Duplicate-prevention contract:
  * If a message is in `messages` AND all its attachments are in
    `attachments` with status='ok' → skip entirely
  * If a message is in `messages` but some attachments are status='fail'
    → retry ONLY the failed attachments (don't re-parse body)
  * If a message is NOT in `messages` → full fetch (body + attachments)
"""
from __future__ import annotations

import logging
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


log = logging.getLogger(__name__)


SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    uid           TEXT PRIMARY KEY,
    folder        TEXT NOT NULL DEFAULT 'inbox',
    subject       TEXT,
    sender        TEXT,
    recv_date     TEXT,     -- ISO-8601
    markdown_path TEXT,     -- relative to project root
    saved_at      INTEGER NOT NULL  -- unix timestamp
);

CREATE INDEX IF NOT EXISTS idx_messages_folder_date
    ON messages (folder, recv_date);

CREATE TABLE IF NOT EXISTS attachments (
    uid           TEXT NOT NULL,      -- parent message uid
    filename      TEXT NOT NULL,      -- sanitized filename
    href          TEXT NOT NULL,      -- original download URL (or path)
    status        TEXT NOT NULL,      -- 'ok' | 'fail' | 'pending'
    size_bytes    INTEGER,            -- reported or actual size
    error_msg     TEXT,               -- last error if status='fail'
    attempts      INTEGER NOT NULL DEFAULT 0,
    local_path    TEXT,               -- relative to project root, NULL if failed
    first_seen    INTEGER NOT NULL,   -- unix ts when first discovered
    last_attempt  INTEGER,            -- unix ts of last attempt
    PRIMARY KEY (uid, filename)
);

CREATE INDEX IF NOT EXISTS idx_attachments_status
    ON attachments (status);

CREATE TABLE IF NOT EXISTS runs (
    run_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   INTEGER NOT NULL,
    finished_at  INTEGER,
    status       TEXT NOT NULL,          -- 'running' | 'ok' | 'fail'
    new_mails    INTEGER NOT NULL DEFAULT 0,
    error        TEXT
);
"""


class StateDB:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)
            c.commit()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    # -------------------------------------------------------- messages

    def has_message(self, uid: str) -> bool:
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM messages WHERE uid = ? LIMIT 1", (uid,)
            ).fetchone()
            return row is not None

    def record_message(
        self,
        uid: str,
        *,
        folder: str,
        subject: str,
        sender: str,
        recv_date: str,
        markdown_path: str,
    ) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT OR REPLACE INTO messages
                   (uid, folder, subject, sender, recv_date, markdown_path, saved_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    uid,
                    folder,
                    subject,
                    sender,
                    recv_date,
                    markdown_path,
                    int(time.time()),
                ),
            )
            c.commit()

    def existing_uids(self, folder: str = "inbox") -> set[str]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT uid FROM messages WHERE folder = ?", (folder,)
            ).fetchall()
            return {r["uid"] for r in rows}

    def message_count(self, folder: str = "inbox") -> int:
        with self._conn() as c:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE folder = ?", (folder,)
            ).fetchone()
            return row["n"] if row else 0

    # ----------------------------------------------------- attachments

    def record_attachment(
        self,
        uid: str,
        *,
        filename: str,
        href: str,
        status: str,
        size_bytes: int | None = None,
        error_msg: str | None = None,
        local_path: str | None = None,
    ) -> None:
        """Upsert an attachment record. Increments `attempts` on every call."""
        with self._conn() as c:
            # Get current attempts count if exists
            row = c.execute(
                "SELECT attempts FROM attachments WHERE uid=? AND filename=?",
                (uid, filename),
            ).fetchone()
            prev_attempts = row["attempts"] if row else 0
            now = int(time.time())

            c.execute(
                """INSERT INTO attachments
                       (uid, filename, href, status, size_bytes, error_msg,
                        attempts, local_path, first_seen, last_attempt)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(uid, filename) DO UPDATE SET
                       href = excluded.href,
                       status = excluded.status,
                       size_bytes = COALESCE(excluded.size_bytes, size_bytes),
                       error_msg = excluded.error_msg,
                       attempts = attempts + 1,
                       local_path = excluded.local_path,
                       last_attempt = excluded.last_attempt""",
                (
                    uid, filename, href, status, size_bytes, error_msg,
                    prev_attempts + 1, local_path, now, now,
                ),
            )
            c.commit()

    def has_attachment_ok(self, uid: str, filename: str) -> bool:
        """Return True if this attachment is already downloaded successfully."""
        with self._conn() as c:
            row = c.execute(
                """SELECT 1 FROM attachments
                   WHERE uid=? AND filename=? AND status='ok' LIMIT 1""",
                (uid, filename),
            ).fetchone()
            return row is not None

    def failed_attachments_for(
        self, uid: str, max_attempts: int = 5
    ) -> list[dict]:
        """Return attachments that failed and still have retry budget."""
        with self._conn() as c:
            rows = c.execute(
                """SELECT uid, filename, href, size_bytes, attempts, error_msg
                   FROM attachments
                   WHERE uid=? AND status='fail' AND attempts < ?""",
                (uid, max_attempts),
            ).fetchall()
            return [dict(r) for r in rows]

    def all_failed_attachments(self, max_attempts: int = 5) -> list[dict]:
        """All retryable failed attachments across the DB."""
        with self._conn() as c:
            rows = c.execute(
                """SELECT uid, filename, href, size_bytes, attempts, error_msg
                   FROM attachments
                   WHERE status='fail' AND attempts < ?
                   ORDER BY first_seen""",
                (max_attempts,),
            ).fetchall()
            return [dict(r) for r in rows]

    def attachment_stats(self) -> dict:
        """Summary: {ok:N, fail:N, pending:N}."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT status, COUNT(*) AS n FROM attachments GROUP BY status"
            ).fetchall()
            stats = {"ok": 0, "fail": 0, "pending": 0}
            for r in rows:
                stats[r["status"]] = r["n"]
            return stats

    # ---------------------------------------------------------- runs

    def start_run(self) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO runs (started_at, status) VALUES (?, 'running')",
                (int(time.time()),),
            )
            c.commit()
            return cur.lastrowid  # type: ignore[return-value]

    def finish_run(
        self, run_id: int, *, status: str, new_mails: int = 0, error: str | None = None
    ) -> None:
        with self._conn() as c:
            c.execute(
                """UPDATE runs SET finished_at = ?, status = ?, new_mails = ?, error = ?
                   WHERE run_id = ?""",
                (int(time.time()), status, new_mails, error, run_id),
            )
            c.commit()
