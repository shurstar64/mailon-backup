"""Entity extraction from ingested mail content.

Extracts and generates wiki pages for:
- People (사람): Names, emails, organizations
- Projects (프로젝트): Project names, codes, references
- Meetings (회의): Meeting titles, dates, attendees

Output structure:
- staging/wiki/people/<name>.md
- staging/wiki/projects/<project>.md
- staging/wiki/meetings/<date>_<title>.md

Usage:
    python -m scripts.extract_entities
    python -m scripts.extract_entities --dry-run
    python -m scripts.extract_entities --type people

Requirements:
    pip install kiwipiepy
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sqlite3
import sys
import time
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterator

try:
    from kiwipiepy import Kiwi
    KIWI_AVAILABLE = True
except ImportError:
    KIWI_AVAILABLE = False

log = logging.getLogger(__name__)

# Initialize Kiwi NLP if available
_kiwi: "Kiwi | None" = None


def get_kiwi() -> "Kiwi | None":
    """Get or initialize Kiwi instance (singleton)."""
    global _kiwi
    if not KIWI_AVAILABLE:
        return None
    if _kiwi is None:
        log.info("Initializing Kiwi NLP...")
        _kiwi = Kiwi()
        # Add common Korean names as user dictionary
        for name in ["이경일", "허신", "김희대", "이영진", "권민우"]:
            _kiwi.add_user_word(name, "NNP")
    return _kiwi

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
STAGING_DIR = PROJECT_ROOT / "staging"
STAGING_MAIL_DIR = STAGING_DIR / "mail"
WIKI_DIR = STAGING_DIR / "wiki"
ENTITY_DB = DATA_DIR / "entities.db"

ENTITY_SCHEMA = """
CREATE TABLE IF NOT EXISTS entities (
    entity_type   TEXT NOT NULL,
    entity_id     TEXT NOT NULL,
    name          TEXT NOT NULL,
    data          TEXT,  -- JSON
    first_seen    INTEGER NOT NULL,
    last_updated  INTEGER NOT NULL,
    mention_count INTEGER DEFAULT 1,
    PRIMARY KEY (entity_type, entity_id)
);

CREATE INDEX IF NOT EXISTS idx_entity_name ON entities (name);
CREATE INDEX IF NOT EXISTS idx_entity_type ON entities (entity_type);

CREATE TABLE IF NOT EXISTS entity_mentions (
    entity_type   TEXT NOT NULL,
    entity_id     TEXT NOT NULL,
    mail_uid      TEXT NOT NULL,
    context       TEXT,
    extracted_at  INTEGER NOT NULL,
    PRIMARY KEY (entity_type, entity_id, mail_uid)
);
"""


@dataclass
class Entity:
    """Base entity class."""
    entity_type: str
    entity_id: str
    name: str
    data: dict = field(default_factory=dict)
    mentions: list[str] = field(default_factory=list)  # list of mail UIDs


@dataclass
class Person(Entity):
    """Person entity."""
    email: str = ""
    organization: str = ""
    title: str = ""


@dataclass
class Project(Entity):
    """Project entity."""
    code: str = ""
    status: str = ""


@dataclass
class Meeting(Entity):
    """Meeting entity."""
    date: str = ""
    attendees: list[str] = field(default_factory=list)
    location: str = ""


class EntityDB:
    """SQLite database for entity storage."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(ENTITY_SCHEMA)
            c.commit()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def upsert_entity(self, entity: Entity) -> None:
        """Insert or update an entity."""
        with self._conn() as c:
            now = int(time.time())
            data_json = json.dumps(entity.data, ensure_ascii=False)

            # Check if exists
            row = c.execute(
                "SELECT mention_count, first_seen FROM entities WHERE entity_type = ? AND entity_id = ?",
                (entity.entity_type, entity.entity_id)
            ).fetchone()

            if row:
                c.execute(
                    """UPDATE entities SET
                       name = ?, data = ?, last_updated = ?, mention_count = mention_count + 1
                       WHERE entity_type = ? AND entity_id = ?""",
                    (entity.name, data_json, now, entity.entity_type, entity.entity_id)
                )
            else:
                c.execute(
                    """INSERT INTO entities (entity_type, entity_id, name, data, first_seen, last_updated, mention_count)
                       VALUES (?, ?, ?, ?, ?, ?, 1)""",
                    (entity.entity_type, entity.entity_id, entity.name, data_json, now, now)
                )
            c.commit()

    def record_mention(self, entity: Entity, mail_uid: str, context: str = "") -> None:
        """Record a mention of an entity in a mail."""
        with self._conn() as c:
            c.execute(
                """INSERT OR REPLACE INTO entity_mentions (entity_type, entity_id, mail_uid, context, extracted_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (entity.entity_type, entity.entity_id, mail_uid, context, int(time.time()))
            )
            c.commit()

    def get_entities_by_type(self, entity_type: str) -> list[dict]:
        """Get all entities of a given type."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM entities WHERE entity_type = ? ORDER BY mention_count DESC",
                (entity_type,)
            ).fetchall()
            return [dict(r) for r in rows]

    def get_stats(self) -> dict:
        """Get entity statistics."""
        with self._conn() as c:
            stats = {}
            for etype in ("person", "project", "meeting"):
                row = c.execute(
                    "SELECT COUNT(*) AS n FROM entities WHERE entity_type = ?",
                    (etype,)
                ).fetchone()
                stats[etype] = row["n"] if row else 0
            return stats


# ---- Pattern Extractors ----

# Email pattern
EMAIL_PATTERN = re.compile(r'[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}')

# Korean name patterns (fallback when Kiwi not available)
KOREAN_NAME_PATTERN = re.compile(r'[가-힣]{2,4}(?:\s*(?:박사|교수|선생님|님|과장|대리|부장|팀장|소장|원장|연구원|주임|담당))?')

# Meeting patterns
MEETING_PATTERNS = [
    re.compile(r'(?:회의|미팅|간담회|세미나|워크샵|발표회|협의회|심의|검토회)'),
    re.compile(r'(?:\d{4}[-./]\d{1,2}[-./]\d{1,2}).*(?:회의|미팅)'),
]

# Project code patterns (common Korean research project formats)
PROJECT_CODE_PATTERN = re.compile(r'(?:[A-Z]{2,5}[-_]?\d{4,}[-_]?\d*|과제번호[:\s]*[\w-]+)')

# Common Korean words that are NOT person names (stopwords for NLP)
KOREAN_NAME_STOPWORDS = {
    # Common verb endings / particles
    "있습니다", "습니다", "합니다", "입니다", "됩니다", "니다",
    "않습니다", "겠습니다", "드립니다", "바랍니다",
    # Common nouns that aren't names
    "안녕", "감사", "회신", "확인", "검토", "참고", "수정", "완료",
    "안내", "문의", "요청", "신청", "등록", "접수", "제출", "발송",
    "메일", "이메일", "경우", "내용", "관련", "사항", "정보",
    "회의", "미팅", "세미나", "워크샵", "발표", "행사",
    "에서", "으로", "에게", "부터", "까지", "대해", "통해",
    # Common organization words
    "학회", "연구원", "연구소", "센터", "대학", "대학교",
    "교수", "박사", "연구", "개발", "기술", "시스템",
    # Single syllables that slip through
    "님", "씨", "분", "건", "중", "후", "전", "내", "외",
}

# Korean title suffixes to strip
KOREAN_TITLE_SUFFIXES = re.compile(
    r'\s*(?:박사|교수|선생님|님|과장|대리|부장|팀장|소장|원장|연구원|주임|담당|위원|이사|사장|대표)$'
)


def normalize_name(name: str) -> str:
    """Normalize a Korean name for deduplication."""
    name = KOREAN_TITLE_SUFFIXES.sub('', name)
    return name.strip()


def entity_id_for(text: str) -> str:
    """Generate a stable entity ID from text."""
    normalized = text.lower().strip()
    return hashlib.md5(normalized.encode()).hexdigest()[:12]


def is_valid_korean_name(name: str) -> bool:
    """Check if a string is likely a valid Korean person name."""
    # Must be 2-4 characters
    if not (2 <= len(name) <= 4):
        return False
    # Must be all Korean
    if not re.match(r'^[가-힣]+$', name):
        return False
    # Not in stopwords
    if name in KOREAN_NAME_STOPWORDS:
        return False
    return True


def extract_people_with_kiwi(content: str, mail_uid: str) -> Iterator[Person]:
    """Extract person entities using Kiwi NLP."""
    kiwi = get_kiwi()
    if kiwi is None:
        return

    seen_names = set()

    # Tokenize with Kiwi
    try:
        tokens = kiwi.tokenize(content)
    except Exception as e:
        log.debug("Kiwi tokenization failed: %s", e)
        return

    for token in tokens:
        # NNP = Proper Noun (고유명사)
        if token.tag == "NNP":
            name = token.form
            if name in seen_names:
                continue
            if not is_valid_korean_name(name):
                continue
            seen_names.add(name)

            yield Person(
                entity_type="person",
                entity_id=entity_id_for(name),
                name=name,
                data={"source": "kiwi_nnp"},
                mentions=[mail_uid],
            )

    # Also look for name patterns with titles (박사, 교수, etc.)
    # These help identify names even if Kiwi didn't tag them as NNP
    title_pattern = re.compile(r'([가-힣]{2,4})\s*(?:박사|교수|선생님|과장|부장|팀장|소장|원장|주임)')
    for match in title_pattern.finditer(content):
        name = match.group(1)
        if name in seen_names:
            continue
        if not is_valid_korean_name(name):
            continue
        seen_names.add(name)

        yield Person(
            entity_type="person",
            entity_id=entity_id_for(name),
            name=name,
            data={"source": "title_pattern"},
            mentions=[mail_uid],
        )


def extract_people_regex(content: str, mail_uid: str) -> Iterator[Person]:
    """Extract Korean names using regex (fallback when Kiwi unavailable)."""
    names = KOREAN_NAME_PATTERN.findall(content)
    seen_names = set()

    for raw_name in names:
        name = normalize_name(raw_name)
        if not is_valid_korean_name(name):
            continue
        if name in seen_names:
            continue
        seen_names.add(name)

        yield Person(
            entity_type="person",
            entity_id=entity_id_for(name),
            name=name,
            data={"raw": raw_name, "source": "regex"},
            mentions=[mail_uid],
        )


def extract_people(content: str, mail_uid: str) -> Iterator[Person]:
    """Extract person entities from mail content."""
    # Extract emails first
    emails = EMAIL_PATTERN.findall(content)
    seen_emails = set()

    for email in emails:
        if email in seen_emails:
            continue
        seen_emails.add(email)

        # Try to find name near email
        name = ""
        # Look for "Name <email>" pattern
        match = re.search(rf'"?([^"<>]+)"?\s*<{re.escape(email)}>', content)
        if match:
            name = match.group(1).strip()

        # Extract domain as organization hint
        org = ""
        domain_match = re.search(r'@([\w.-]+)', email)
        if domain_match:
            domain = domain_match.group(1)
            if domain.endswith('.re.kr') or domain.endswith('.ac.kr'):
                org = domain.split('.')[0].upper()

        yield Person(
            entity_type="person",
            entity_id=entity_id_for(email),
            name=name or email.split('@')[0],
            email=email,
            organization=org,
            data={"email": email, "organization": org},
            mentions=[mail_uid],
        )

    # Extract Korean names using Kiwi NLP (preferred) or regex fallback
    if KIWI_AVAILABLE:
        yield from extract_people_with_kiwi(content, mail_uid)
    else:
        log.debug("Kiwi not available, using regex fallback")
        yield from extract_people_regex(content, mail_uid)


def extract_projects(content: str, mail_uid: str) -> Iterator[Project]:
    """Extract project entities from mail content."""
    codes = PROJECT_CODE_PATTERN.findall(content)
    seen = set()

    for code in codes:
        normalized = code.strip()
        if normalized in seen:
            continue
        seen.add(normalized)

        yield Project(
            entity_type="project",
            entity_id=entity_id_for(normalized),
            name=normalized,
            code=normalized,
            data={"code": normalized},
            mentions=[mail_uid],
        )


def extract_meetings(content: str, mail_uid: str, mail_date: str = "") -> Iterator[Meeting]:
    """Extract meeting entities from mail content."""
    for pattern in MEETING_PATTERNS:
        matches = pattern.findall(content)
        for match in matches:
            # Try to extract date
            date_match = re.search(r'(\d{4}[-./]\d{1,2}[-./]\d{1,2})', match)
            date = date_match.group(1) if date_match else mail_date

            # Clean up meeting title
            title = match[:50]  # Limit length

            yield Meeting(
                entity_type="meeting",
                entity_id=entity_id_for(f"{date}_{title}"),
                name=title,
                date=date,
                data={"title": title, "date": date},
                mentions=[mail_uid],
            )


def generate_person_wiki(person: Person, mention_count: int) -> str:
    """Generate wiki page content for a person."""
    lines = [
        "---",
        f"type: person",
        f"name: {person.name}",
        f"email: {person.email}",
        f"organization: {person.organization}",
        f"mentions: {mention_count}",
        "---",
        "",
        f"# {person.name}",
        "",
    ]

    if person.email:
        lines.append(f"**Email**: {person.email}  ")
    if person.organization:
        lines.append(f"**Organization**: {person.organization}  ")

    lines.append("")
    lines.append("## Related Mails")
    lines.append("")
    lines.append("<!-- Auto-generated list will be populated here -->")
    lines.append("")

    return "\n".join(lines)


def generate_project_wiki(project: Project, mention_count: int) -> str:
    """Generate wiki page content for a project."""
    lines = [
        "---",
        f"type: project",
        f"name: {project.name}",
        f"code: {project.code}",
        f"mentions: {mention_count}",
        "---",
        "",
        f"# {project.name}",
        "",
        f"**Project Code**: {project.code}  ",
        "",
        "## Related Mails",
        "",
        "<!-- Auto-generated list will be populated here -->",
        "",
    ]
    return "\n".join(lines)


def generate_meeting_wiki(meeting: Meeting, mention_count: int) -> str:
    """Generate wiki page content for a meeting."""
    lines = [
        "---",
        f"type: meeting",
        f"name: {meeting.name}",
        f"date: {meeting.date}",
        f"mentions: {mention_count}",
        "---",
        "",
        f"# {meeting.name}",
        "",
        f"**Date**: {meeting.date}  ",
        "",
        "## Related Mails",
        "",
        "<!-- Auto-generated list will be populated here -->",
        "",
    ]
    return "\n".join(lines)


def slugify(text: str, max_len: int = 50) -> str:
    """Create filesystem-safe slug."""
    # Remove special characters but keep Korean
    text = re.sub(r'[\\/:*?"<>|]', '', text)
    text = re.sub(r'\s+', '_', text.strip())
    if len(text) > max_len:
        text = text[:max_len]
    return text or "untitled"


def iter_staging_mails() -> Iterator[tuple[Path, str, str]]:
    """Iterate over staging mail files, yielding (path, content, uid)."""
    if not STAGING_MAIL_DIR.exists():
        log.warning("Staging mail directory does not exist: %s", STAGING_MAIL_DIR)
        return

    for md_file in STAGING_MAIL_DIR.rglob("*.md"):
        try:
            content = md_file.read_text(encoding="utf-8")
            # Extract UID from frontmatter
            uid_match = re.search(r'^uid:\s*(.+)$', content, re.MULTILINE)
            uid = uid_match.group(1).strip() if uid_match else md_file.stem
            yield md_file, content, uid
        except Exception as e:
            log.warning("Failed to read %s: %s", md_file, e)


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Extract entities from mail content")
    parser.add_argument("--dry-run", action="store_true", help="Don't write any files")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    parser.add_argument("--type", choices=["people", "projects", "meetings"], help="Extract only specific type")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    log.info("Starting entity extraction...")
    log.info("Source: %s", STAGING_MAIL_DIR)
    log.info("Wiki output: %s", WIKI_DIR)

    if args.dry_run:
        log.info("DRY RUN - no files will be written")

    # Initialize database
    db = EntityDB(ENTITY_DB)

    # Track entities
    people: dict[str, Person] = {}
    projects: dict[str, Project] = {}
    meetings: dict[str, Meeting] = {}

    stats = {"mails_processed": 0, "people": 0, "projects": 0, "meetings": 0}

    # Process all staging mails
    for mail_path, content, uid in iter_staging_mails():
        stats["mails_processed"] += 1

        # Extract date from content
        date_match = re.search(r'^date:\s*(.+)$', content, re.MULTILINE)
        mail_date = date_match.group(1).strip() if date_match else ""

        # Extract entities
        if not args.type or args.type == "people":
            for person in extract_people(content, uid):
                if person.entity_id in people:
                    people[person.entity_id].mentions.append(uid)
                else:
                    people[person.entity_id] = person

        if not args.type or args.type == "projects":
            for project in extract_projects(content, uid):
                if project.entity_id in projects:
                    projects[project.entity_id].mentions.append(uid)
                else:
                    projects[project.entity_id] = project

        if not args.type or args.type == "meetings":
            for meeting in extract_meetings(content, uid, mail_date):
                if meeting.entity_id in meetings:
                    meetings[meeting.entity_id].mentions.append(uid)
                else:
                    meetings[meeting.entity_id] = meeting

    log.info("Extracted: %d people, %d projects, %d meetings",
             len(people), len(projects), len(meetings))

    # Write wiki pages
    if not args.dry_run:
        # People
        people_dir = WIKI_DIR / "people"
        people_dir.mkdir(parents=True, exist_ok=True)
        for person in people.values():
            db.upsert_entity(person)
            for uid in person.mentions:
                db.record_mention(person, uid)

            wiki_content = generate_person_wiki(person, len(person.mentions))
            wiki_path = people_dir / f"{slugify(person.name)}.md"
            wiki_path.write_text(wiki_content, encoding="utf-8")
            stats["people"] += 1

        # Projects
        projects_dir = WIKI_DIR / "projects"
        projects_dir.mkdir(parents=True, exist_ok=True)
        for project in projects.values():
            db.upsert_entity(project)
            for uid in project.mentions:
                db.record_mention(project, uid)

            wiki_content = generate_project_wiki(project, len(project.mentions))
            wiki_path = projects_dir / f"{slugify(project.name)}.md"
            wiki_path.write_text(wiki_content, encoding="utf-8")
            stats["projects"] += 1

        # Meetings
        meetings_dir = WIKI_DIR / "meetings"
        meetings_dir.mkdir(parents=True, exist_ok=True)
        for meeting in meetings.values():
            db.upsert_entity(meeting)
            for uid in meeting.mentions:
                db.record_mention(meeting, uid)

            wiki_content = generate_meeting_wiki(meeting, len(meeting.mentions))
            wiki_path = meetings_dir / f"{slugify(meeting.date)}_{slugify(meeting.name)}.md"
            wiki_path.write_text(wiki_content, encoding="utf-8")
            stats["meetings"] += 1

    log.info("Entity extraction complete:")
    log.info("  Mails processed: %d", stats["mails_processed"])
    log.info("  People pages:    %d", stats["people"])
    log.info("  Project pages:   %d", stats["projects"])
    log.info("  Meeting pages:   %d", stats["meetings"])

    return 0


if __name__ == "__main__":
    sys.exit(main())
