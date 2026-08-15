"""Autophagy backend adapter for MailON integration.

Provides a standardized interface for LLM Wiki (Autophagy) to access
mail data from mailon-backup. Implements the Autophagy Backend Protocol.

Features:
- Mail retrieval by UID, date range, sender, keyword
- Entity resolution (people, projects, meetings)
- Full-text search with relevance scoring
- Incremental sync status reporting
- Batch operations for efficiency

Usage:
    from adapters.mailon_backend import MailonBackend

    backend = MailonBackend()
    mails = backend.search("이경일 박사 프로젝트")
    person = backend.get_entity("person", "이경일")
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import sys
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, Protocol, TypedDict

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
STATE_DB = DATA_DIR / "state.db"
INGEST_DB = DATA_DIR / "ingest.db"
ENTITY_DB = DATA_DIR / "entities.db"
STAGING_DIR = PROJECT_ROOT / "staging"
STAGING_MAIL_DIR = STAGING_DIR / "mail"


# ---- Data Types ----

@dataclass
class MailRecord:
    """Represents a mail record."""
    uid: str
    folder: str
    subject: str
    sender: str
    to: str
    cc: str
    date: datetime | None
    body: str
    attachments: list[str]
    staging_path: str
    content_hash: str


@dataclass
class EntityRecord:
    """Represents an entity record."""
    entity_type: str
    entity_id: str
    name: str
    data: dict
    mention_count: int
    first_seen: datetime | None
    last_updated: datetime | None


@dataclass
class SearchResult:
    """Search result with relevance scoring."""
    mail: MailRecord
    relevance: float
    matched_terms: list[str]
    snippet: str


@dataclass
class SyncStatus:
    """Sync status information."""
    last_sync: datetime | None
    total_messages: int
    total_attachments: int
    attachment_success_rate: float
    last_run_status: str
    ingested_count: int
    entity_count: int


# ---- Backend Protocol ----

class AutophagyBackend(Protocol):
    """Protocol for Autophagy backend implementations."""

    def get_mail(self, uid: str) -> MailRecord | None:
        """Get a single mail by UID."""
        ...

    def list_mails(
        self,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        sender: str | None = None,
        folder: str = "inbox",
        limit: int = 100,
        offset: int = 0,
    ) -> list[MailRecord]:
        """List mails with optional filters."""
        ...

    def search(
        self,
        query: str,
        limit: int = 20,
    ) -> list[SearchResult]:
        """Full-text search with natural language."""
        ...

    def get_entity(self, entity_type: str, entity_id: str) -> EntityRecord | None:
        """Get entity by type and ID."""
        ...

    def list_entities(
        self,
        entity_type: str,
        limit: int = 100,
    ) -> list[EntityRecord]:
        """List entities of a given type."""
        ...

    def get_sync_status(self) -> SyncStatus:
        """Get current sync status."""
        ...


# ---- Implementation ----

class MailonBackend:
    """Autophagy backend implementation for mailon-backup."""

    def __init__(self, project_root: Path | None = None):
        self.project_root = project_root or PROJECT_ROOT
        self.data_dir = self.project_root / "data"
        self.staging_dir = self.project_root / "staging"
        self._state_db = self.data_dir / "state.db"
        self._ingest_db = self.data_dir / "ingest.db"
        self._entity_db = self.data_dir / "entities.db"

    @contextmanager
    def _conn(self, db_path: Path):
        """Context manager for database connections."""
        if not db_path.exists():
            yield None
            return
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _parse_mail_content(self, staging_path: Path) -> dict:
        """Parse staging mail content."""
        if not staging_path.exists():
            return {}

        try:
            content = staging_path.read_text(encoding="utf-8")
        except Exception:
            return {}

        # Parse YAML frontmatter
        if not content.startswith("---"):
            return {"body": content}

        lines = content.split("\n")
        end_idx = None
        for i, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                end_idx = i
                break

        if end_idx is None:
            return {"body": content}

        frontmatter = "\n".join(lines[1:end_idx])
        body = "\n".join(lines[end_idx + 1:])

        # Simple key-value parsing
        data = {"body": body}
        for line in frontmatter.split("\n"):
            if ":" in line:
                key, val = line.split(":", 1)
                data[key.strip()] = val.strip()

        return data

    def get_mail(self, uid: str) -> MailRecord | None:
        """Get a single mail by UID."""
        with self._conn(self._ingest_db) as conn:
            if conn is None:
                return None

            row = conn.execute(
                "SELECT * FROM ingested_files WHERE uid = ?",
                (uid,)
            ).fetchone()

            if not row:
                return None

            staging_path = self.project_root / row["staging_path"]
            content = self._parse_mail_content(staging_path)

            return MailRecord(
                uid=row["uid"],
                folder=content.get("folder", "inbox"),
                subject=row["subject"] or "",
                sender=row["sender"] or "",
                to=content.get("to", ""),
                cc=content.get("cc", ""),
                date=self._parse_date(row["recv_date"]),
                body=content.get("body", ""),
                attachments=self._parse_attachments(content.get("attachments", "[]")),
                staging_path=row["staging_path"],
                content_hash=row["content_hash"],
            )

    def _parse_date(self, date_str: str | None) -> datetime | None:
        """Parse ISO date string."""
        if not date_str:
            return None
        try:
            return datetime.fromisoformat(date_str.replace('"', '').strip())
        except (ValueError, TypeError):
            return None

    def _parse_attachments(self, att_str: str) -> list[str]:
        """Parse attachments string."""
        try:
            if att_str.startswith("["):
                return eval(att_str)
            return []
        except Exception:
            return []

    def list_mails(
        self,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        sender: str | None = None,
        folder: str = "inbox",
        limit: int = 100,
        offset: int = 0,
    ) -> list[MailRecord]:
        """List mails with optional filters."""
        with self._conn(self._ingest_db) as conn:
            if conn is None:
                return []

            query = "SELECT * FROM ingested_files WHERE 1=1"
            params: list[Any] = []

            if start_date:
                query += " AND recv_date >= ?"
                params.append(start_date.isoformat())

            if end_date:
                query += " AND recv_date < ?"
                params.append(end_date.isoformat())

            if sender:
                query += " AND sender LIKE ?"
                params.append(f"%{sender}%")

            query += " ORDER BY recv_date DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])

            rows = conn.execute(query, params).fetchall()

            results = []
            for row in rows:
                staging_path = self.project_root / row["staging_path"]
                content = self._parse_mail_content(staging_path)

                results.append(MailRecord(
                    uid=row["uid"],
                    folder=content.get("folder", folder),
                    subject=row["subject"] or "",
                    sender=row["sender"] or "",
                    to=content.get("to", ""),
                    cc=content.get("cc", ""),
                    date=self._parse_date(row["recv_date"]),
                    body=content.get("body", ""),
                    attachments=self._parse_attachments(content.get("attachments", "[]")),
                    staging_path=row["staging_path"],
                    content_hash=row["content_hash"],
                ))

            return results

    def search(
        self,
        query: str,
        limit: int = 20,
    ) -> list[SearchResult]:
        """Full-text search with natural language."""
        # Import search module
        from scripts.search_mail import parse_query, search as do_search

        parsed = parse_query(query)
        raw_results = do_search(parsed, limit=limit)

        results = []
        for r in raw_results:
            mail = self.get_mail(r.uid)
            if mail:
                results.append(SearchResult(
                    mail=mail,
                    relevance=r.relevance,
                    matched_terms=r.matched_terms,
                    snippet=r.snippet,
                ))

        return results

    def get_entity(self, entity_type: str, entity_id: str) -> EntityRecord | None:
        """Get entity by type and ID."""
        with self._conn(self._entity_db) as conn:
            if conn is None:
                return None

            row = conn.execute(
                "SELECT * FROM entities WHERE entity_type = ? AND entity_id = ?",
                (entity_type, entity_id)
            ).fetchone()

            if not row:
                return None

            return EntityRecord(
                entity_type=row["entity_type"],
                entity_id=row["entity_id"],
                name=row["name"],
                data=json.loads(row["data"]) if row["data"] else {},
                mention_count=row["mention_count"],
                first_seen=datetime.fromtimestamp(row["first_seen"]) if row["first_seen"] else None,
                last_updated=datetime.fromtimestamp(row["last_updated"]) if row["last_updated"] else None,
            )

    def find_entity_by_name(self, entity_type: str, name: str) -> EntityRecord | None:
        """Find entity by name (fuzzy match)."""
        with self._conn(self._entity_db) as conn:
            if conn is None:
                return None

            row = conn.execute(
                "SELECT * FROM entities WHERE entity_type = ? AND name LIKE ? ORDER BY mention_count DESC LIMIT 1",
                (entity_type, f"%{name}%")
            ).fetchone()

            if not row:
                return None

            return EntityRecord(
                entity_type=row["entity_type"],
                entity_id=row["entity_id"],
                name=row["name"],
                data=json.loads(row["data"]) if row["data"] else {},
                mention_count=row["mention_count"],
                first_seen=datetime.fromtimestamp(row["first_seen"]) if row["first_seen"] else None,
                last_updated=datetime.fromtimestamp(row["last_updated"]) if row["last_updated"] else None,
            )

    def list_entities(
        self,
        entity_type: str,
        limit: int = 100,
    ) -> list[EntityRecord]:
        """List entities of a given type."""
        with self._conn(self._entity_db) as conn:
            if conn is None:
                return []

            rows = conn.execute(
                "SELECT * FROM entities WHERE entity_type = ? ORDER BY mention_count DESC LIMIT ?",
                (entity_type, limit)
            ).fetchall()

            return [
                EntityRecord(
                    entity_type=row["entity_type"],
                    entity_id=row["entity_id"],
                    name=row["name"],
                    data=json.loads(row["data"]) if row["data"] else {},
                    mention_count=row["mention_count"],
                    first_seen=datetime.fromtimestamp(row["first_seen"]) if row["first_seen"] else None,
                    last_updated=datetime.fromtimestamp(row["last_updated"]) if row["last_updated"] else None,
                )
                for row in rows
            ]

    def get_entity_mentions(self, entity_type: str, entity_id: str) -> list[str]:
        """Get mail UIDs where entity is mentioned."""
        with self._conn(self._entity_db) as conn:
            if conn is None:
                return []

            rows = conn.execute(
                "SELECT mail_uid FROM entity_mentions WHERE entity_type = ? AND entity_id = ?",
                (entity_type, entity_id)
            ).fetchall()

            return [row["mail_uid"] for row in rows]

    def get_sync_status(self) -> SyncStatus:
        """Get current sync status."""
        last_sync = None
        total_messages = 0
        total_attachments = 0
        attachment_success_rate = 0.0
        last_run_status = "unknown"
        ingested_count = 0
        entity_count = 0

        # State DB stats
        with self._conn(self._state_db) as conn:
            if conn:
                # Messages
                row = conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()
                total_messages = row["n"] if row else 0

                # Attachments
                att_stats = conn.execute(
                    "SELECT status, COUNT(*) AS n FROM attachments GROUP BY status"
                ).fetchall()
                stats = {r["status"]: r["n"] for r in att_stats}
                total_attachments = sum(stats.values())
                ok_count = stats.get("ok", 0)
                attachment_success_rate = ok_count / total_attachments if total_attachments > 0 else 0

                # Last run
                last_run = conn.execute(
                    "SELECT * FROM runs ORDER BY run_id DESC LIMIT 1"
                ).fetchone()
                if last_run:
                    last_run_status = last_run["status"]
                    if last_run["finished_at"]:
                        last_sync = datetime.fromtimestamp(last_run["finished_at"])

        # Ingest DB stats
        with self._conn(self._ingest_db) as conn:
            if conn:
                row = conn.execute("SELECT COUNT(*) AS n FROM ingested_files").fetchone()
                ingested_count = row["n"] if row else 0

        # Entity DB stats
        with self._conn(self._entity_db) as conn:
            if conn:
                row = conn.execute("SELECT COUNT(*) AS n FROM entities").fetchone()
                entity_count = row["n"] if row else 0

        return SyncStatus(
            last_sync=last_sync,
            total_messages=total_messages,
            total_attachments=total_attachments,
            attachment_success_rate=attachment_success_rate,
            last_run_status=last_run_status,
            ingested_count=ingested_count,
            entity_count=entity_count,
        )

    def get_mails_for_entity(self, entity_type: str, entity_id: str) -> list[MailRecord]:
        """Get all mails mentioning an entity."""
        uids = self.get_entity_mentions(entity_type, entity_id)
        return [mail for uid in uids if (mail := self.get_mail(uid)) is not None]

    def to_dict(self, obj: Any) -> dict:
        """Convert dataclass to dict."""
        if hasattr(obj, "__dataclass_fields__"):
            return asdict(obj)
        return {}


# ---- CLI for testing ----

def main() -> int:
    """CLI entry point for testing."""
    import argparse

    parser = argparse.ArgumentParser(description="MailON Backend CLI")
    parser.add_argument("command", choices=["status", "list", "search", "entity"])
    parser.add_argument("--query", "-q", help="Search query")
    parser.add_argument("--type", help="Entity type")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    backend = MailonBackend()

    if args.command == "status":
        status = backend.get_sync_status()
        if args.json:
            print(json.dumps({
                "last_sync": status.last_sync.isoformat() if status.last_sync else None,
                "total_messages": status.total_messages,
                "total_attachments": status.total_attachments,
                "attachment_success_rate": round(status.attachment_success_rate * 100, 1),
                "last_run_status": status.last_run_status,
                "ingested_count": status.ingested_count,
                "entity_count": status.entity_count,
            }, indent=2))
        else:
            print(f"Last Sync: {status.last_sync}")
            print(f"Messages: {status.total_messages}")
            print(f"Attachments: {status.total_attachments} ({status.attachment_success_rate*100:.1f}% success)")
            print(f"Ingested: {status.ingested_count}")
            print(f"Entities: {status.entity_count}")

    elif args.command == "list":
        mails = backend.list_mails(limit=args.limit)
        for mail in mails:
            print(f"[{mail.uid}] {mail.subject}")
            print(f"    From: {mail.sender}")
            print(f"    Date: {mail.date}")

    elif args.command == "search":
        if not args.query:
            print("Error: --query required for search")
            return 1
        results = backend.search(args.query, limit=args.limit)
        for r in results:
            print(f"[{r.relevance:.1f}] {r.mail.subject}")
            print(f"    Matched: {', '.join(r.matched_terms)}")

    elif args.command == "entity":
        if not args.type:
            print("Error: --type required for entity")
            return 1
        entities = backend.list_entities(args.type, limit=args.limit)
        for e in entities:
            print(f"[{e.mention_count}] {e.name}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
