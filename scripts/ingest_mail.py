"""Incremental mail ingestion for LLM Wiki.

Uses SHA-256 content hashes to detect changes and avoid re-processing.
Reads from data/mails/*.md (READ-ONLY) and writes normalized content to staging/mail/.

The ingest.db tracks:
- file_path: relative path to source markdown
- content_hash: SHA-256 of file content
- ingested_at: timestamp of last ingestion
- staging_path: path to normalized output

Usage:
    # Full ingestion
    python -m scripts.ingest_mail

    # Dry run (no writes)
    python -m scripts.ingest_mail --dry-run

    # Verbose output
    python -m scripts.ingest_mail --verbose
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import re
import sqlite3
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator

import yaml

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
MAILS_DIR = DATA_DIR / "mails"
STAGING_DIR = PROJECT_ROOT / "staging"
STAGING_MAIL_DIR = STAGING_DIR / "mail"
INGEST_DB = DATA_DIR / "ingest.db"

INGEST_SCHEMA = """
CREATE TABLE IF NOT EXISTS ingested_files (
    file_path     TEXT PRIMARY KEY,
    content_hash  TEXT NOT NULL,
    ingested_at   INTEGER NOT NULL,
    staging_path  TEXT NOT NULL,
    uid           TEXT,
    subject       TEXT,
    sender        TEXT,
    recv_date     TEXT
);

CREATE INDEX IF NOT EXISTS idx_ingested_hash ON ingested_files (content_hash);
CREATE INDEX IF NOT EXISTS idx_ingested_date ON ingested_files (recv_date);
"""


@dataclass
class ParsedMail:
    """Parsed mail metadata and content."""
    uid: str
    folder: str
    subject: str
    sender: str
    to: str
    cc: str
    date: str
    attachments: list[str]
    body: str
    source_path: Path


class IngestDB:
    """SQLite database for tracking ingested files."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(INGEST_SCHEMA)
            c.commit()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def get_hash(self, file_path: str) -> str | None:
        """Get stored hash for a file path."""
        with self._conn() as c:
            row = c.execute(
                "SELECT content_hash FROM ingested_files WHERE file_path = ?",
                (file_path,)
            ).fetchone()
            return row["content_hash"] if row else None

    def record_ingestion(
        self,
        file_path: str,
        content_hash: str,
        staging_path: str,
        uid: str | None = None,
        subject: str | None = None,
        sender: str | None = None,
        recv_date: str | None = None,
    ) -> None:
        """Record that a file has been ingested."""
        with self._conn() as c:
            c.execute(
                """INSERT OR REPLACE INTO ingested_files
                   (file_path, content_hash, ingested_at, staging_path, uid, subject, sender, recv_date)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (file_path, content_hash, int(time.time()), staging_path, uid, subject, sender, recv_date)
            )
            c.commit()

    def get_stats(self) -> dict:
        """Get ingestion statistics."""
        with self._conn() as c:
            total = c.execute("SELECT COUNT(*) AS n FROM ingested_files").fetchone()["n"]
            return {"total_ingested": total}


def compute_hash(content: bytes) -> str:
    """Compute SHA-256 hash of content."""
    return hashlib.sha256(content).hexdigest()


def fix_malformed_yaml(yaml_str: str) -> str:
    """Fix common YAML syntax issues from mailon's writer.

    Handles:
    - attachments:[] → attachments: []
    - key:{} → key: {}
    """
    # Fix missing space after colon before [ or {
    yaml_str = re.sub(r':(\[)', r': \1', yaml_str)
    yaml_str = re.sub(r':(\{)', r': \1', yaml_str)
    return yaml_str


def parse_yaml_frontmatter(content: str) -> tuple[dict, str]:
    """Parse YAML frontmatter and body from markdown content."""
    if not content.startswith("---"):
        return {}, content

    # Find closing ---
    lines = content.split("\n")
    end_idx = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end_idx = i
            break

    if end_idx is None:
        return {}, content

    frontmatter_str = "\n".join(lines[1:end_idx])
    body = "\n".join(lines[end_idx + 1:])

    # Fix malformed YAML from mailon's writer
    frontmatter_str = fix_malformed_yaml(frontmatter_str)

    try:
        frontmatter = yaml.safe_load(frontmatter_str) or {}
    except yaml.YAMLError:
        frontmatter = {}

    return frontmatter, body


def parse_mail_file(file_path: Path) -> ParsedMail | None:
    """Parse a mail markdown file."""
    try:
        content = file_path.read_text(encoding="utf-8")
    except Exception as e:
        log.error("Failed to read %s: %s", file_path, e)
        return None

    frontmatter, body = parse_yaml_frontmatter(content)

    # Extract metadata
    uid = str(frontmatter.get("uid", ""))
    if not uid:
        # Try to extract from filename: YYYY-MM-DD_slug_uid.md
        match = re.search(r"_(\d+)\.md$", file_path.name)
        uid = match.group(1) if match else ""

    attachments = frontmatter.get("attachments", [])
    if isinstance(attachments, str):
        attachments = [attachments]
    elif not isinstance(attachments, list):
        attachments = []

    return ParsedMail(
        uid=uid,
        folder=str(frontmatter.get("folder", "inbox")),
        subject=str(frontmatter.get("subject", "")),
        sender=str(frontmatter.get("from", "")),
        to=str(frontmatter.get("to", "")),
        cc=str(frontmatter.get("cc", "")),
        date=str(frontmatter.get("date", "")),
        attachments=attachments,
        body=body.strip(),
        source_path=file_path,
    )


def normalize_mail(mail: ParsedMail) -> str:
    """Generate normalized markdown content for staging."""
    lines = [
        "---",
        f"uid: {mail.uid}",
        f"folder: {mail.folder}",
        f"subject: |",
        f"  {mail.subject}",
        f"from: {mail.sender}",
        f"to: {mail.to}",
        f"cc: {mail.cc}",
        f"date: {mail.date}",
        f"source: {mail.source_path.name}",
        f"attachments: {mail.attachments}",
        "---",
        "",
        mail.body,
        "",
    ]
    return "\n".join(lines)


def staging_path_for(mail: ParsedMail) -> Path:
    """Compute staging path for a mail."""
    # Parse date from frontmatter
    dt = None
    try:
        dt = datetime.fromisoformat(mail.date.replace('"', ''))
    except (ValueError, AttributeError):
        pass

    # Fallback: extract date from filename (YYYY-MM-DD_subject_uid.md)
    if dt is None:
        filename = mail.source_path.name
        match = re.match(r"(\d{4})-(\d{2})-(\d{2})_", filename)
        if match:
            try:
                dt = datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            except ValueError:
                pass

    # Final fallback: use source file's parent directory structure
    if dt is None:
        # Try to extract from path like data/mails/2026/03/file.md
        parts = mail.source_path.parts
        for i, part in enumerate(parts):
            if part == "mails" and i + 2 < len(parts):
                try:
                    year = int(parts[i + 1])
                    month = int(parts[i + 2])
                    dt = datetime(year, month, 1)
                    break
                except (ValueError, IndexError):
                    pass

    if dt is None:
        dt = datetime.now()

    yyyy = f"{dt.year:04d}"
    mm = f"{dt.month:02d}"

    # Use same filename as source
    filename = mail.source_path.name
    return STAGING_MAIL_DIR / yyyy / mm / filename


def is_staging_file(file_path: Path) -> bool:
    """Check if file is a staging file (not an original mailon file)."""
    try:
        content = file_path.read_text(encoding="utf-8")[:500]
        # Staging files have 'source:' field, originals have 'collected_at:'
        return "source:" in content and "collected_at:" not in content
    except Exception:
        return False


def iter_mail_files() -> Iterator[Path]:
    """Iterate over all mail markdown files (excluding staging-style files)."""
    if not MAILS_DIR.exists():
        log.warning("Mails directory does not exist: %s", MAILS_DIR)
        return

    for md_file in MAILS_DIR.rglob("*.md"):
        # Skip README and staging files
        if md_file.name == "README.md":
            continue
        if is_staging_file(md_file):
            log.debug("Skipping staging file: %s", md_file.name)
            continue
        yield md_file


def ingest_mail(
    mail_path: Path,
    db: IngestDB,
    dry_run: bool = False,
    force: bool = False,
) -> tuple[bool, str]:
    """
    Ingest a single mail file.

    Returns (was_updated, reason).
    """
    rel_path = str(mail_path.relative_to(PROJECT_ROOT))

    # Read file and compute hash
    try:
        content = mail_path.read_bytes()
    except Exception as e:
        return False, f"read error: {e}"

    content_hash = compute_hash(content)

    # Check if already ingested with same hash (skip if force)
    stored_hash = db.get_hash(rel_path)
    if not force and stored_hash == content_hash:
        return False, "unchanged"

    # Parse mail
    mail = parse_mail_file(mail_path)
    if not mail:
        return False, "parse error"

    # Generate normalized content
    normalized = normalize_mail(mail)
    staging_path = staging_path_for(mail)
    staging_rel = str(staging_path.relative_to(PROJECT_ROOT))

    if not dry_run:
        # Write to staging
        staging_path.parent.mkdir(parents=True, exist_ok=True)
        staging_path.write_text(normalized, encoding="utf-8")

        # Record in database
        db.record_ingestion(
            file_path=rel_path,
            content_hash=content_hash,
            staging_path=staging_rel,
            uid=mail.uid,
            subject=mail.subject,
            sender=mail.sender,
            recv_date=mail.date,
        )

    status = "new" if stored_hash is None else "updated"
    return True, status


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Ingest mail files into LLM Wiki staging")
    parser.add_argument("--dry-run", action="store_true", help="Don't write any files")
    parser.add_argument("--force", action="store_true", help="Re-ingest all files, ignore hash check")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    log.info("Starting mail ingestion...")
    log.info("Source: %s", MAILS_DIR)
    log.info("Staging: %s", STAGING_MAIL_DIR)

    if args.dry_run:
        log.info("DRY RUN - no files will be written")
    if args.force:
        log.info("FORCE mode - re-ingesting all files")

    # Initialize database
    db = IngestDB(INGEST_DB)

    # Process all mail files
    stats = {"processed": 0, "new": 0, "updated": 0, "unchanged": 0, "errors": 0}

    for mail_path in iter_mail_files():
        stats["processed"] += 1
        was_updated, reason = ingest_mail(mail_path, db, dry_run=args.dry_run, force=args.force)

        if was_updated:
            if reason == "new":
                stats["new"] += 1
            else:
                stats["updated"] += 1
            log.debug("%s: %s", mail_path.name, reason)
        elif reason == "unchanged":
            stats["unchanged"] += 1
        else:
            stats["errors"] += 1
            log.warning("%s: %s", mail_path.name, reason)

    log.info("Ingestion complete:")
    log.info("  Processed: %d", stats["processed"])
    log.info("  New:       %d", stats["new"])
    log.info("  Updated:   %d", stats["updated"])
    log.info("  Unchanged: %d", stats["unchanged"])
    log.info("  Errors:    %d", stats["errors"])

    return 0


if __name__ == "__main__":
    sys.exit(main())
