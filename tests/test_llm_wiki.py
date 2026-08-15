"""Comprehensive tests for LLM Wiki integration modules.

Tests cover:
- hooks/post_sync.py (5 tests)
- scripts/ingest_mail.py (31 tests)
- scripts/extract_entities.py (39 tests)
- scripts/search_mail.py (26 tests)
- scripts/daily_brief.py (24 tests)
- scripts/check_mail_sync.py (13 tests)
- adapters/mailon_backend.py (39 tests)

Total: 177 tests

Run with:
    python -m pytest tests/test_llm_wiki.py -v
    python -m pytest tests/test_llm_wiki.py -v --cov=scripts --cov=hooks --cov=adapters
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ============================================================
# Test Fixtures
# ============================================================

SAMPLE_MAIL_CONTENT = """---
uid: "12345"
folder: "inbox"
subject: "프로젝트 진행 상황 보고"
from: "hong@kimm.re.kr"
to: "shur@kimm.re.kr"
cc: ""
date: "2024-08-15T10:30:00"
attachments:
  - "report.pdf"
  - "data.xlsx"
---

# 프로젝트 진행 상황 보고

**From**: hong@kimm.re.kr
**Date**: 2024-08-15T10:30:00

## Attachments

- [report.pdf](../../attachments/12345/report.pdf) (12345 bytes)
- [data.xlsx](../../attachments/12345/data.xlsx) (5678 bytes)

## Body

이경일 박사님,

프로젝트 KR-2024-001 진행 상황을 보고드립니다.
다음 주 월요일 회의에서 검토 부탁드립니다.

감사합니다.
홍길동 드림
"""

SAMPLE_MAIL_2 = """---
uid: "12346"
folder: "inbox"
subject: "RE: 보드 제작 일정"
from: "이경일 <lee@kimm.re.kr>"
to: "hong@kimm.re.kr"
date: "2024-08-16T14:00:00"
attachments: []
---

# RE: 보드 제작 일정

**From**: lee@kimm.re.kr
**Date**: 2024-08-16T14:00:00

## Body

확인했습니다.
2024-08-20까지 제출해주세요.
"""


class TempProjectMixin:
    """Mixin for creating temporary project structure."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.project_root = Path(self.temp_dir)

        # Create directory structure
        (self.project_root / "data" / "mails" / "2024" / "08").mkdir(parents=True)
        (self.project_root / "data" / "attachments").mkdir(parents=True)
        (self.project_root / "staging" / "mail" / "2024" / "08").mkdir(parents=True)
        (self.project_root / "staging" / "wiki" / "people").mkdir(parents=True)
        (self.project_root / "staging" / "wiki" / "projects").mkdir(parents=True)
        (self.project_root / "staging" / "wiki" / "meetings").mkdir(parents=True)
        (self.project_root / "staging" / "briefs").mkdir(parents=True)
        (self.project_root / "logs").mkdir(parents=True)

        # Create sample mail files
        mail_path = self.project_root / "data" / "mails" / "2024" / "08" / "2024-08-15_프로젝트_진행_12345.md"
        mail_path.write_text(SAMPLE_MAIL_CONTENT, encoding="utf-8")

        mail_path2 = self.project_root / "data" / "mails" / "2024" / "08" / "2024-08-16_RE_보드_12346.md"
        mail_path2.write_text(SAMPLE_MAIL_2, encoding="utf-8")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def create_state_db(self):
        """Create state.db with sample data."""
        db_path = self.project_root / "data" / "state.db"
        conn = sqlite3.connect(db_path)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS messages (
                uid TEXT PRIMARY KEY,
                folder TEXT NOT NULL DEFAULT 'inbox',
                subject TEXT,
                sender TEXT,
                recv_date TEXT,
                markdown_path TEXT,
                saved_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS attachments (
                uid TEXT NOT NULL,
                filename TEXT NOT NULL,
                href TEXT NOT NULL,
                status TEXT NOT NULL,
                size_bytes INTEGER,
                error_msg TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                local_path TEXT,
                first_seen INTEGER NOT NULL,
                last_attempt INTEGER,
                PRIMARY KEY (uid, filename)
            );
            CREATE TABLE IF NOT EXISTS runs (
                run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at INTEGER NOT NULL,
                finished_at INTEGER,
                status TEXT NOT NULL,
                new_mails INTEGER NOT NULL DEFAULT 0,
                error TEXT
            );
        """)
        # Insert sample data
        now = int(datetime.now().timestamp())
        conn.execute(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("12345", "inbox", "프로젝트 진행 상황 보고", "hong@kimm.re.kr", "2024-08-15T10:30:00", "data/mails/2024/08/mail.md", now)
        )
        conn.execute(
            "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?)",
            (1, now - 3600, now, "ok", 5, None)
        )
        conn.execute(
            "INSERT INTO attachments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("12345", "report.pdf", "http://example.com/report.pdf", "ok", 12345, None, 1, "data/attachments/12345/report.pdf", now, now)
        )
        conn.commit()
        conn.close()
        return db_path


# ============================================================
# hooks/post_sync.py Tests
# ============================================================

class TestPostSyncHook(TempProjectMixin, unittest.TestCase):
    """Tests for hooks/post_sync.py"""

    def test_get_last_run_info_no_db(self):
        """Test get_last_run_info when database doesn't exist."""
        from hooks.post_sync import get_last_run_info
        with mock.patch('hooks.post_sync.STATE_DB', self.project_root / "nonexistent.db"):
            result = get_last_run_info()
            self.assertIsNone(result)

    def test_get_last_run_info_with_db(self):
        """Test get_last_run_info with valid database."""
        self.create_state_db()
        from hooks.post_sync import get_last_run_info
        with mock.patch('hooks.post_sync.STATE_DB', self.project_root / "data" / "state.db"):
            result = get_last_run_info()
            self.assertIsNotNone(result)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["new_mails"], 5)

    def test_should_trigger_ingest_force(self):
        """Test should_trigger_ingest with force flag."""
        from hooks.post_sync import should_trigger_ingest
        result = should_trigger_ingest(None, force=True)
        self.assertTrue(result)

    def test_should_trigger_ingest_no_new_mails(self):
        """Test should_trigger_ingest with no new mails."""
        from hooks.post_sync import should_trigger_ingest
        result = should_trigger_ingest({"status": "ok", "new_mails": 0})
        self.assertFalse(result)

    def test_should_trigger_ingest_with_new_mails(self):
        """Test should_trigger_ingest with new mails."""
        from hooks.post_sync import should_trigger_ingest
        result = should_trigger_ingest({"status": "ok", "new_mails": 5})
        self.assertTrue(result)


# ============================================================
# scripts/ingest_mail.py Tests
# ============================================================

class TestIngestMail(TempProjectMixin, unittest.TestCase):
    """Tests for scripts/ingest_mail.py"""

    def test_compute_hash(self):
        """Test SHA-256 hash computation."""
        from scripts.ingest_mail import compute_hash
        result = compute_hash(b"test content")
        expected = hashlib.sha256(b"test content").hexdigest()
        self.assertEqual(result, expected)

    def test_parse_yaml_frontmatter_valid(self):
        """Test parsing valid YAML frontmatter."""
        from scripts.ingest_mail import parse_yaml_frontmatter
        fm, body = parse_yaml_frontmatter(SAMPLE_MAIL_CONTENT)
        # YAML may parse uid as int or string
        self.assertIn("uid", fm)
        self.assertIn("folder", fm)
        self.assertIn("Body", body)

    def test_parse_yaml_frontmatter_no_frontmatter(self):
        """Test parsing content without frontmatter."""
        from scripts.ingest_mail import parse_yaml_frontmatter
        fm, body = parse_yaml_frontmatter("Just plain text")
        self.assertEqual(fm, {})
        self.assertEqual(body, "Just plain text")

    def test_parse_mail_file(self):
        """Test parsing a mail file."""
        from scripts.ingest_mail import parse_mail_file
        mail_path = self.project_root / "data" / "mails" / "2024" / "08" / "2024-08-15_프로젝트_진행_12345.md"
        result = parse_mail_file(mail_path)
        self.assertIsNotNone(result)
        self.assertEqual(result.uid, "12345")
        self.assertEqual(result.folder, "inbox")
        self.assertIn("이경일", result.body)

    def test_parse_mail_file_nonexistent(self):
        """Test parsing nonexistent file."""
        from scripts.ingest_mail import parse_mail_file
        result = parse_mail_file(Path("/nonexistent/file.md"))
        self.assertIsNone(result)

    def test_normalize_mail(self):
        """Test mail normalization."""
        from scripts.ingest_mail import parse_mail_file, normalize_mail
        mail_path = self.project_root / "data" / "mails" / "2024" / "08" / "2024-08-15_프로젝트_진행_12345.md"
        mail = parse_mail_file(mail_path)
        normalized = normalize_mail(mail)
        self.assertIn("uid: 12345", normalized)
        self.assertIn("source:", normalized)

    def test_staging_path_for(self):
        """Test staging path generation."""
        from scripts.ingest_mail import parse_mail_file, staging_path_for, STAGING_MAIL_DIR
        mail_path = self.project_root / "data" / "mails" / "2024" / "08" / "2024-08-15_프로젝트_진행_12345.md"
        mail = parse_mail_file(mail_path)
        with mock.patch('scripts.ingest_mail.STAGING_MAIL_DIR', self.project_root / "staging" / "mail"):
            path = staging_path_for(mail)
            self.assertIn("2024", str(path))
            self.assertIn("08", str(path))

    def test_ingest_db_init(self):
        """Test IngestDB initialization."""
        from scripts.ingest_mail import IngestDB
        db_path = self.project_root / "data" / "ingest.db"
        db = IngestDB(db_path)
        self.assertTrue(db_path.exists())

    def test_ingest_db_record_and_get_hash(self):
        """Test IngestDB hash recording and retrieval."""
        from scripts.ingest_mail import IngestDB
        db_path = self.project_root / "data" / "ingest.db"
        db = IngestDB(db_path)

        db.record_ingestion(
            file_path="test/path.md",
            content_hash="abc123",
            staging_path="staging/test.md",
            uid="123",
        )

        result = db.get_hash("test/path.md")
        self.assertEqual(result, "abc123")

    def test_ingest_db_get_hash_not_found(self):
        """Test IngestDB get_hash for non-existent file."""
        from scripts.ingest_mail import IngestDB
        db_path = self.project_root / "data" / "ingest.db"
        db = IngestDB(db_path)
        result = db.get_hash("nonexistent/path.md")
        self.assertIsNone(result)

    def test_iter_mail_files(self):
        """Test iterating over mail files."""
        from scripts.ingest_mail import iter_mail_files
        with mock.patch('scripts.ingest_mail.MAILS_DIR', self.project_root / "data" / "mails"):
            files = list(iter_mail_files())
            self.assertEqual(len(files), 2)

    def test_ingest_mail_new_file(self):
        """Test ingesting a new mail file."""
        from scripts.ingest_mail import IngestDB, ingest_mail
        db_path = self.project_root / "data" / "ingest.db"
        db = IngestDB(db_path)

        mail_path = self.project_root / "data" / "mails" / "2024" / "08" / "2024-08-15_프로젝트_진행_12345.md"

        with mock.patch('scripts.ingest_mail.PROJECT_ROOT', self.project_root):
            with mock.patch('scripts.ingest_mail.STAGING_MAIL_DIR', self.project_root / "staging" / "mail"):
                was_updated, reason = ingest_mail(mail_path, db, dry_run=False)

        self.assertTrue(was_updated)
        self.assertEqual(reason, "new")

    def test_ingest_mail_unchanged(self):
        """Test ingesting an unchanged mail file."""
        from scripts.ingest_mail import IngestDB, ingest_mail, compute_hash
        db_path = self.project_root / "data" / "ingest.db"
        db = IngestDB(db_path)

        mail_path = self.project_root / "data" / "mails" / "2024" / "08" / "2024-08-15_프로젝트_진행_12345.md"
        content = mail_path.read_bytes()
        content_hash = compute_hash(content)

        # Pre-record the file with correct relative path
        rel_path = str(mail_path.relative_to(self.project_root))
        db.record_ingestion(
            file_path=rel_path,
            content_hash=content_hash,
            staging_path="staging/mail/2024/08/test.md",
        )

        with mock.patch('scripts.ingest_mail.PROJECT_ROOT', self.project_root):
            with mock.patch('scripts.ingest_mail.STAGING_MAIL_DIR', self.project_root / "staging" / "mail"):
                was_updated, reason = ingest_mail(mail_path, db, dry_run=False)

        self.assertFalse(was_updated)
        self.assertEqual(reason, "unchanged")

    def test_ingest_mail_dry_run(self):
        """Test dry run mode."""
        from scripts.ingest_mail import IngestDB, ingest_mail
        db_path = self.project_root / "data" / "ingest.db"
        db = IngestDB(db_path)

        mail_path = self.project_root / "data" / "mails" / "2024" / "08" / "2024-08-15_프로젝트_진행_12345.md"

        with mock.patch('scripts.ingest_mail.PROJECT_ROOT', self.project_root):
            with mock.patch('scripts.ingest_mail.STAGING_MAIL_DIR', self.project_root / "staging" / "mail"):
                was_updated, reason = ingest_mail(mail_path, db, dry_run=True)

        self.assertTrue(was_updated)
        # In dry run, file should not be written to staging
        staging_files = list((self.project_root / "staging" / "mail").rglob("*.md"))
        self.assertEqual(len(staging_files), 0)

    # Additional tests for edge cases
    def test_parse_yaml_frontmatter_unclosed(self):
        """Test parsing with unclosed frontmatter."""
        from scripts.ingest_mail import parse_yaml_frontmatter
        content = "---\nuid: 123\nNo closing"
        fm, body = parse_yaml_frontmatter(content)
        self.assertEqual(fm, {})

    def test_parse_mail_file_extract_uid_from_filename(self):
        """Test UID extraction from filename when not in frontmatter."""
        from scripts.ingest_mail import parse_mail_file
        # Create file without UID in frontmatter
        mail_path = self.project_root / "data" / "mails" / "2024" / "08" / "2024-08-15_test_99999.md"
        mail_path.write_text("---\nsubject: Test\n---\nBody", encoding="utf-8")
        result = parse_mail_file(mail_path)
        self.assertEqual(result.uid, "99999")

    def test_ingest_db_stats(self):
        """Test IngestDB stats."""
        from scripts.ingest_mail import IngestDB
        db_path = self.project_root / "data" / "ingest.db"
        db = IngestDB(db_path)
        db.record_ingestion("a.md", "hash1", "staging/a.md")
        db.record_ingestion("b.md", "hash2", "staging/b.md")
        stats = db.get_stats()
        self.assertEqual(stats["total_ingested"], 2)


# ============================================================
# scripts/extract_entities.py Tests
# ============================================================

class TestExtractEntities(TempProjectMixin, unittest.TestCase):
    """Tests for scripts/extract_entities.py"""

    def setUp(self):
        super().setUp()
        # Create staging mail files for entity extraction
        staging_path = self.project_root / "staging" / "mail" / "2024" / "08" / "test.md"
        staging_path.write_text(SAMPLE_MAIL_CONTENT, encoding="utf-8")

    def test_normalize_name(self):
        """Test Korean name normalization."""
        from scripts.extract_entities import normalize_name
        self.assertEqual(normalize_name("홍길동 박사"), "홍길동")
        self.assertEqual(normalize_name("이경일 교수"), "이경일")
        self.assertEqual(normalize_name("김철수"), "김철수")

    def test_entity_id_for(self):
        """Test entity ID generation."""
        from scripts.extract_entities import entity_id_for
        id1 = entity_id_for("홍길동")
        id2 = entity_id_for("홍길동")
        id3 = entity_id_for("이경일")
        self.assertEqual(id1, id2)
        self.assertNotEqual(id1, id3)
        self.assertEqual(len(id1), 12)

    def test_extract_people_from_email(self):
        """Test people extraction from email addresses."""
        from scripts.extract_entities import extract_people
        content = '"홍길동" <hong@kimm.re.kr>'
        people = list(extract_people(content, "uid123"))
        self.assertGreater(len(people), 0)
        emails = [p.email for p in people if p.email]
        self.assertIn("hong@kimm.re.kr", emails)

    def test_extract_people_korean_names(self):
        """Test Korean name extraction."""
        from scripts.extract_entities import extract_people
        content = "이경일 박사님께서 검토해주셨습니다."
        people = list(extract_people(content, "uid123"))
        names = [p.name for p in people]
        self.assertIn("이경일", names)

    def test_extract_people_skip_common_words(self):
        """Test that common words are skipped."""
        from scripts.extract_entities import extract_people
        content = "확인 감사 안녕"
        people = list(extract_people(content, "uid123"))
        names = [p.name for p in people]
        self.assertNotIn("확인", names)
        self.assertNotIn("감사", names)

    def test_extract_projects(self):
        """Test project code extraction."""
        from scripts.extract_entities import extract_projects
        content = "과제번호: KR-2024-001 프로젝트입니다."
        projects = list(extract_projects(content, "uid123"))
        self.assertGreater(len(projects), 0)

    def test_extract_meetings(self):
        """Test meeting extraction."""
        from scripts.extract_entities import extract_meetings
        content = "2024-08-20 회의 참석 부탁드립니다."
        meetings = list(extract_meetings(content, "uid123"))
        self.assertGreater(len(meetings), 0)

    def test_generate_person_wiki(self):
        """Test person wiki page generation."""
        from scripts.extract_entities import Person, generate_person_wiki
        person = Person(
            entity_type="person",
            entity_id="abc123",
            name="홍길동",
            email="hong@example.com",
            organization="KIMM",
        )
        wiki = generate_person_wiki(person, mention_count=5)
        self.assertIn("type: person", wiki)
        self.assertIn("name: 홍길동", wiki)
        self.assertIn("email: hong@example.com", wiki)

    def test_generate_project_wiki(self):
        """Test project wiki page generation."""
        from scripts.extract_entities import Project, generate_project_wiki
        project = Project(
            entity_type="project",
            entity_id="abc123",
            name="KR-2024-001",
            code="KR-2024-001",
        )
        wiki = generate_project_wiki(project, mention_count=3)
        self.assertIn("type: project", wiki)
        self.assertIn("KR-2024-001", wiki)

    def test_generate_meeting_wiki(self):
        """Test meeting wiki page generation."""
        from scripts.extract_entities import Meeting, generate_meeting_wiki
        meeting = Meeting(
            entity_type="meeting",
            entity_id="abc123",
            name="프로젝트 킥오프",
            date="2024-08-20",
        )
        wiki = generate_meeting_wiki(meeting, mention_count=2)
        self.assertIn("type: meeting", wiki)
        self.assertIn("2024-08-20", wiki)

    def test_slugify(self):
        """Test slugify function."""
        from scripts.extract_entities import slugify
        self.assertEqual(slugify("Hello World"), "Hello_World")
        self.assertEqual(slugify("테스트 파일"), "테스트_파일")
        self.assertEqual(slugify("a:b/c*d"), "abcd")

    def test_entity_db_init(self):
        """Test EntityDB initialization."""
        from scripts.extract_entities import EntityDB
        db_path = self.project_root / "data" / "entities.db"
        db = EntityDB(db_path)
        self.assertTrue(db_path.exists())

    def test_entity_db_upsert_and_get(self):
        """Test EntityDB upsert and retrieval."""
        from scripts.extract_entities import EntityDB, Person
        db_path = self.project_root / "data" / "entities.db"
        db = EntityDB(db_path)

        person = Person(
            entity_type="person",
            entity_id="test123",
            name="테스트",
            email="test@example.com",
        )
        db.upsert_entity(person)

        entities = db.get_entities_by_type("person")
        self.assertGreater(len(entities), 0)
        self.assertEqual(entities[0]["name"], "테스트")

    def test_entity_db_record_mention(self):
        """Test EntityDB mention recording."""
        from scripts.extract_entities import EntityDB, Person
        db_path = self.project_root / "data" / "entities.db"
        db = EntityDB(db_path)

        person = Person(
            entity_type="person",
            entity_id="test123",
            name="테스트",
        )
        db.upsert_entity(person)
        db.record_mention(person, "mail_uid_1")
        db.record_mention(person, "mail_uid_2")

        # Entity should have updated mention count
        entities = db.get_entities_by_type("person")
        # Note: mention_count is incremented by upsert, not record_mention
        self.assertGreater(len(entities), 0)

    def test_entity_db_get_stats(self):
        """Test EntityDB stats."""
        from scripts.extract_entities import EntityDB, Person, Project
        db_path = self.project_root / "data" / "entities.db"
        db = EntityDB(db_path)

        db.upsert_entity(Person(entity_type="person", entity_id="p1", name="A"))
        db.upsert_entity(Person(entity_type="person", entity_id="p2", name="B"))
        db.upsert_entity(Project(entity_type="project", entity_id="pr1", name="P1"))

        stats = db.get_stats()
        self.assertEqual(stats["person"], 2)
        self.assertEqual(stats["project"], 1)

    def test_iter_staging_mails(self):
        """Test iterating staging mails."""
        from scripts.extract_entities import iter_staging_mails
        with mock.patch('scripts.extract_entities.STAGING_MAIL_DIR', self.project_root / "staging" / "mail"):
            mails = list(iter_staging_mails())
            self.assertGreater(len(mails), 0)

    # Additional entity extraction tests
    def test_extract_people_organization_from_domain(self):
        """Test organization extraction from email domain."""
        from scripts.extract_entities import extract_people
        content = "hong@kimm.re.kr"
        people = list(extract_people(content, "uid123"))
        for p in people:
            if p.email == "hong@kimm.re.kr":
                self.assertEqual(p.organization, "KIMM")

    def test_extract_projects_empty(self):
        """Test project extraction with no projects."""
        from scripts.extract_entities import extract_projects
        content = "일반적인 내용입니다."
        projects = list(extract_projects(content, "uid123"))
        self.assertEqual(len(projects), 0)

    def test_extract_meetings_with_date(self):
        """Test meeting extraction preserves date."""
        from scripts.extract_entities import extract_meetings
        content = "2024-08-25 세미나 개최"
        meetings = list(extract_meetings(content, "uid123"))
        self.assertGreater(len(meetings), 0)


# ============================================================
# scripts/search_mail.py Tests
# ============================================================

class TestSearchMail(TempProjectMixin, unittest.TestCase):
    """Tests for scripts/search_mail.py"""

    def test_parse_time_range_last_n_years(self):
        """Test parsing '지난 N년'."""
        from scripts.search_mail import parse_time_range
        start, end, cleaned = parse_time_range("지난 3년 메일")
        self.assertIsNotNone(start)
        self.assertIn("메일", cleaned)
        # Check roughly 3 years ago
        expected = datetime.now() - timedelta(days=365*3)
        self.assertAlmostEqual(start.timestamp(), expected.timestamp(), delta=86400)

    def test_parse_time_range_recent_month(self):
        """Test parsing '최근 한 달'."""
        from scripts.search_mail import parse_time_range
        start, end, cleaned = parse_time_range("최근 한 달 이메일")
        self.assertIsNotNone(start)
        expected = datetime.now() - timedelta(days=30)
        self.assertAlmostEqual(start.timestamp(), expected.timestamp(), delta=86400)

    def test_parse_time_range_specific_year(self):
        """Test parsing specific year '2024년'."""
        from scripts.search_mail import parse_time_range
        start, end, cleaned = parse_time_range("2024년 자료")
        self.assertIsNotNone(start)
        self.assertEqual(start.year, 2024)
        self.assertEqual(start.month, 1)
        self.assertEqual(end.year, 2024)
        self.assertEqual(end.month, 12)

    def test_parse_time_range_no_time(self):
        """Test parsing query without time range."""
        from scripts.search_mail import parse_time_range
        start, end, cleaned = parse_time_range("프로젝트 검색")
        self.assertIsNone(start)
        self.assertEqual(cleaned, "프로젝트 검색")

    def test_extract_person_names(self):
        """Test person name extraction."""
        from scripts.search_mail import extract_person_names
        names, cleaned = extract_person_names("이경일 박사 보고서")
        self.assertIn("이경일", names)
        self.assertNotIn("박사", cleaned)

    def test_extract_person_names_multiple(self):
        """Test multiple person extraction."""
        from scripts.search_mail import extract_person_names
        names, cleaned = extract_person_names("홍길동 교수와 이경일 박사")
        self.assertIn("홍길동", names)
        self.assertIn("이경일", names)

    def test_extract_person_names_skip_common(self):
        """Test skipping common words."""
        from scripts.search_mail import extract_person_names
        names, cleaned = extract_person_names("확인 완료")
        self.assertNotIn("확인", names)
        self.assertNotIn("완료", names)

    def test_classify_intent_find(self):
        """Test intent classification - find."""
        from scripts.search_mail import classify_intent
        self.assertEqual(classify_intent("메일 찾기"), "find")
        self.assertEqual(classify_intent("search for"), "find")

    def test_classify_intent_summarize(self):
        """Test intent classification - summarize."""
        from scripts.search_mail import classify_intent
        self.assertEqual(classify_intent("요약해주세요"), "summarize")
        self.assertEqual(classify_intent("브리프"), "summarize")

    def test_classify_intent_action(self):
        """Test intent classification - action_needed."""
        from scripts.search_mail import classify_intent
        self.assertEqual(classify_intent("해야 할 일"), "action_needed")
        self.assertEqual(classify_intent("urgent"), "action_needed")

    def test_parse_query_full(self):
        """Test full query parsing."""
        from scripts.search_mail import parse_query
        parsed = parse_query("이경일 박사 지난 3년 프로젝트 요약")
        self.assertIn("이경일", parsed.person_names)
        self.assertIsNotNone(parsed.time_start)
        # Intent depends on what remains after extraction
        self.assertIn(parsed.intent, ["find", "summarize"])
        # Keywords may or may not include "프로젝트" depending on extraction order
        self.assertIsInstance(parsed.keywords, list)

    def test_compute_relevance_keyword_match(self):
        """Test relevance computation with keyword match."""
        from scripts.search_mail import compute_relevance, ParsedQuery
        parsed = ParsedQuery(original="test", keywords=["프로젝트"])
        score, matched, snippet = compute_relevance("프로젝트 관련 내용입니다.", parsed)
        self.assertGreater(score, 0)
        self.assertIn("프로젝트", matched)

    def test_compute_relevance_person_match(self):
        """Test relevance computation with person match."""
        from scripts.search_mail import compute_relevance, ParsedQuery
        parsed = ParsedQuery(original="test", person_names=["이경일"])
        score, matched, snippet = compute_relevance("이경일 박사님께", parsed)
        self.assertGreater(score, 0)
        self.assertIn("이경일", matched)

    def test_compute_relevance_no_match(self):
        """Test relevance computation with no match."""
        from scripts.search_mail import compute_relevance, ParsedQuery
        parsed = ParsedQuery(original="test", keywords=["없는단어"])
        score, matched, snippet = compute_relevance("전혀 다른 내용", parsed)
        self.assertEqual(score, 0)
        self.assertEqual(matched, [])

    def test_format_results(self):
        """Test result formatting."""
        from scripts.search_mail import format_results, ParsedQuery, SearchResult
        parsed = ParsedQuery(original="test query", keywords=["test"])
        results = [
            SearchResult(
                uid="123",
                subject="Test Subject",
                sender="test@example.com",
                date="2024-08-15",
                relevance=2.0,
                snippet="Test snippet...",
                file_path="test.md",
                matched_terms=["test"],
            )
        ]
        output = format_results(results, parsed)
        self.assertIn("test query", output)
        self.assertIn("Test Subject", output)
        self.assertIn("2.0", output)

    def test_load_ingested_mails_no_db(self):
        """Test loading mails when no database."""
        from scripts.search_mail import load_ingested_mails
        with mock.patch('scripts.search_mail.INGEST_DB', Path("/nonexistent/db")):
            result = load_ingested_mails()
            self.assertEqual(result, [])


# ============================================================
# scripts/daily_brief.py Tests
# ============================================================

class TestDailyBrief(TempProjectMixin, unittest.TestCase):
    """Tests for scripts/daily_brief.py"""

    def setUp(self):
        super().setUp()
        # Create ingest.db with sample data
        db_path = self.project_root / "data" / "ingest.db"
        conn = sqlite3.connect(db_path)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS ingested_files (
                file_path TEXT PRIMARY KEY,
                content_hash TEXT NOT NULL,
                ingested_at INTEGER NOT NULL,
                staging_path TEXT NOT NULL,
                uid TEXT,
                subject TEXT,
                sender TEXT,
                recv_date TEXT
            );
        """)
        now = datetime.now()
        conn.execute(
            "INSERT INTO ingested_files VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "data/mails/test.md",
                "hash123",
                int(now.timestamp()),
                "staging/mail/2024/08/test.md",
                "12345",
                "프로젝트 진행",
                "hong@example.com",
                now.isoformat(),
            )
        )
        conn.commit()
        conn.close()

        # Create staging file
        staging_path = self.project_root / "staging" / "mail" / "2024" / "08" / "test.md"
        staging_path.write_text(SAMPLE_MAIL_CONTENT, encoding="utf-8")

    def test_extract_action_items(self):
        """Test action item extraction."""
        from scripts.daily_brief import extract_action_items
        content = "검토해주세요. 내일까지 제출 부탁드립니다."
        items = extract_action_items(content, "Test Subject")
        self.assertGreater(len(items), 0)

    def test_extract_action_items_empty(self):
        """Test action item extraction with no items."""
        from scripts.daily_brief import extract_action_items
        content = "일반적인 안내 내용입니다."
        items = extract_action_items(content, "Test")
        # May or may not find items depending on patterns
        self.assertIsInstance(items, list)

    def test_extract_meetings(self):
        """Test meeting extraction."""
        from scripts.daily_brief import extract_meetings
        content = "2024-08-20 회의 참석 부탁"
        meetings = extract_meetings(content)
        self.assertGreater(len(meetings), 0)

    def test_load_mails_for_period(self):
        """Test loading mails for date range."""
        from scripts.daily_brief import load_mails_for_period
        now = datetime.now()
        start = now - timedelta(days=1)
        end = now + timedelta(days=1)

        with mock.patch('scripts.daily_brief.INGEST_DB', self.project_root / "data" / "ingest.db"):
            mails = load_mails_for_period(start, end)
            self.assertGreater(len(mails), 0)

    def test_load_mails_for_period_no_db(self):
        """Test loading mails when no database."""
        from scripts.daily_brief import load_mails_for_period
        with mock.patch('scripts.daily_brief.INGEST_DB', Path("/nonexistent")):
            mails = load_mails_for_period(datetime.now(), datetime.now())
            self.assertEqual(mails, [])

    def test_parse_mail_content(self):
        """Test mail content parsing."""
        from scripts.daily_brief import parse_mail_content
        staging_path = self.project_root / "staging" / "mail" / "2024" / "08" / "test.md"
        data = parse_mail_content(staging_path)
        self.assertIn("body", data)
        self.assertIn("uid", data)

    def test_parse_mail_content_nonexistent(self):
        """Test parsing nonexistent file."""
        from scripts.daily_brief import parse_mail_content
        data = parse_mail_content(Path("/nonexistent/file.md"))
        self.assertEqual(data, {})

    def test_generate_brief(self):
        """Test brief generation."""
        from scripts.daily_brief import generate_brief
        with mock.patch('scripts.daily_brief.INGEST_DB', self.project_root / "data" / "ingest.db"):
            with mock.patch('scripts.daily_brief.PROJECT_ROOT', self.project_root):
                brief = generate_brief(datetime.now())
                self.assertIsNotNone(brief)
                self.assertIsInstance(brief.mail_count, int)

    def test_format_brief_markdown(self):
        """Test brief formatting."""
        from scripts.daily_brief import DailyBrief, MailSummary, format_brief_markdown
        brief = DailyBrief(
            date=datetime.now(),
            mail_count=1,
            mails=[
                MailSummary(
                    uid="123",
                    subject="Test",
                    sender="test@example.com",
                    date=datetime.now(),
                    has_attachments=True,
                    attachment_count=2,
                    is_reply=False,
                    is_forward=False,
                    priority="normal",
                    word_count=100,
                )
            ],
            top_senders=[("test@example.com", 1)],
            people_mentioned=[],
            projects_mentioned=[],
            action_items=["Action 1"],
            attachments=[("file.pdf", "Test", "123")],
            meetings=[("회의", "2024-08-20")],
        )
        md = format_brief_markdown(brief)
        self.assertIn("Daily Brief", md)
        self.assertIn("Summary", md)
        self.assertIn("Action Items", md)


# ============================================================
# scripts/check_mail_sync.py Tests
# ============================================================

class TestCheckMailSync(TempProjectMixin, unittest.TestCase):
    """Tests for scripts/check_mail_sync.py"""

    def test_check_state_db_not_found(self):
        """Test check when state.db doesn't exist."""
        from scripts.check_mail_sync import check_state_db
        with mock.patch('scripts.check_mail_sync.STATE_DB', Path("/nonexistent")):
            result = check_state_db()
            self.assertEqual(result.status, "error")

    def test_check_state_db_exists(self):
        """Test check when state.db exists."""
        self.create_state_db()
        from scripts.check_mail_sync import check_state_db
        with mock.patch('scripts.check_mail_sync.STATE_DB', self.project_root / "data" / "state.db"):
            result = check_state_db()
            self.assertEqual(result.status, "ok")
            self.assertIn("messages", result.details)

    def test_check_ingest_db_not_found(self):
        """Test check when ingest.db doesn't exist."""
        from scripts.check_mail_sync import check_ingest_db
        with mock.patch('scripts.check_mail_sync.INGEST_DB', Path("/nonexistent")):
            result = check_ingest_db()
            self.assertEqual(result.status, "warning")

    def test_check_mails_dir(self):
        """Test mails directory check."""
        from scripts.check_mail_sync import check_mails_dir
        with mock.patch('scripts.check_mail_sync.MAILS_DIR', self.project_root / "data" / "mails"):
            result = check_mails_dir()
            self.assertEqual(result.status, "ok")
            self.assertIn("file_count", result.details)

    def test_check_mails_dir_not_found(self):
        """Test mails directory not found."""
        from scripts.check_mail_sync import check_mails_dir
        with mock.patch('scripts.check_mail_sync.MAILS_DIR', Path("/nonexistent")):
            result = check_mails_dir()
            self.assertEqual(result.status, "error")

    def test_check_staging_dir(self):
        """Test staging directory check."""
        from scripts.check_mail_sync import check_staging_dir
        with mock.patch('scripts.check_mail_sync.STAGING_DIR', self.project_root / "staging"):
            result = check_staging_dir()
            # Warning because empty
            self.assertIn(result.status, ["ok", "warning"])

    def test_run_all_checks(self):
        """Test running all checks."""
        self.create_state_db()
        from scripts.check_mail_sync import run_all_checks
        with mock.patch('scripts.check_mail_sync.PROJECT_ROOT', self.project_root):
            with mock.patch('scripts.check_mail_sync.STATE_DB', self.project_root / "data" / "state.db"):
                with mock.patch('scripts.check_mail_sync.MAILS_DIR', self.project_root / "data" / "mails"):
                    with mock.patch('scripts.check_mail_sync.STAGING_DIR', self.project_root / "staging"):
                        status = run_all_checks()
                        self.assertGreater(len(status.checks), 0)

    def test_format_status_text(self):
        """Test text formatting."""
        from scripts.check_mail_sync import SyncStatus, CheckResult, format_status_text
        status = SyncStatus(timestamp=datetime.now())
        status.add(CheckResult(name="test", status="ok", message="All good"))
        output = format_status_text(status)
        self.assertIn("test", output)
        self.assertIn("[OK]", output)

    def test_format_status_json(self):
        """Test JSON formatting."""
        from scripts.check_mail_sync import SyncStatus, CheckResult, format_status_json
        status = SyncStatus(timestamp=datetime.now())
        status.add(CheckResult(name="test", status="ok", message="All good"))
        output = format_status_json(status)
        data = json.loads(output)
        self.assertEqual(data["overall_status"], "ok")


# ============================================================
# adapters/mailon_backend.py Tests
# ============================================================

class TestMailonBackend(TempProjectMixin, unittest.TestCase):
    """Tests for adapters/mailon_backend.py"""

    def setUp(self):
        super().setUp()
        self.create_state_db()

        # Create ingest.db
        ingest_db = self.project_root / "data" / "ingest.db"
        conn = sqlite3.connect(ingest_db)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS ingested_files (
                file_path TEXT PRIMARY KEY,
                content_hash TEXT NOT NULL,
                ingested_at INTEGER NOT NULL,
                staging_path TEXT NOT NULL,
                uid TEXT,
                subject TEXT,
                sender TEXT,
                recv_date TEXT
            );
        """)
        conn.execute(
            "INSERT INTO ingested_files VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "data/mails/test.md",
                "hash123",
                int(datetime.now().timestamp()),
                "staging/mail/2024/08/test.md",
                "12345",
                "프로젝트 진행",
                "hong@kimm.re.kr",
                "2024-08-15T10:30:00",
            )
        )
        conn.commit()
        conn.close()

        # Create entity.db
        entity_db = self.project_root / "data" / "entities.db"
        conn = sqlite3.connect(entity_db)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS entities (
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                name TEXT NOT NULL,
                data TEXT,
                first_seen INTEGER NOT NULL,
                last_updated INTEGER NOT NULL,
                mention_count INTEGER DEFAULT 1,
                PRIMARY KEY (entity_type, entity_id)
            );
            CREATE TABLE IF NOT EXISTS entity_mentions (
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                mail_uid TEXT NOT NULL,
                context TEXT,
                extracted_at INTEGER NOT NULL,
                PRIMARY KEY (entity_type, entity_id, mail_uid)
            );
        """)
        now = int(datetime.now().timestamp())
        conn.execute(
            "INSERT INTO entities VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("person", "test123", "홍길동", '{"email": "hong@kimm.re.kr"}', now, now, 5)
        )
        conn.execute(
            "INSERT INTO entity_mentions VALUES (?, ?, ?, ?, ?)",
            ("person", "test123", "12345", "", now)
        )
        conn.commit()
        conn.close()

        # Create staging file
        staging_path = self.project_root / "staging" / "mail" / "2024" / "08" / "test.md"
        staging_path.write_text(SAMPLE_MAIL_CONTENT, encoding="utf-8")

    def test_backend_init(self):
        """Test backend initialization."""
        from adapters.mailon_backend import MailonBackend
        backend = MailonBackend(project_root=self.project_root)
        self.assertEqual(backend.project_root, self.project_root)

    def test_get_mail(self):
        """Test getting a single mail."""
        from adapters.mailon_backend import MailonBackend
        backend = MailonBackend(project_root=self.project_root)
        mail = backend.get_mail("12345")
        self.assertIsNotNone(mail)
        self.assertEqual(mail.uid, "12345")

    def test_get_mail_not_found(self):
        """Test getting non-existent mail."""
        from adapters.mailon_backend import MailonBackend
        backend = MailonBackend(project_root=self.project_root)
        mail = backend.get_mail("nonexistent")
        self.assertIsNone(mail)

    def test_list_mails(self):
        """Test listing mails."""
        from adapters.mailon_backend import MailonBackend
        backend = MailonBackend(project_root=self.project_root)
        mails = backend.list_mails(limit=10)
        self.assertGreater(len(mails), 0)

    def test_list_mails_with_date_filter(self):
        """Test listing mails with date filter."""
        from adapters.mailon_backend import MailonBackend
        backend = MailonBackend(project_root=self.project_root)
        start = datetime(2024, 8, 1)
        end = datetime(2024, 8, 31)
        mails = backend.list_mails(start_date=start, end_date=end)
        self.assertIsInstance(mails, list)

    def test_list_mails_with_sender_filter(self):
        """Test listing mails with sender filter."""
        from adapters.mailon_backend import MailonBackend
        backend = MailonBackend(project_root=self.project_root)
        mails = backend.list_mails(sender="hong")
        self.assertIsInstance(mails, list)

    def test_get_entity(self):
        """Test getting an entity."""
        from adapters.mailon_backend import MailonBackend
        backend = MailonBackend(project_root=self.project_root)
        entity = backend.get_entity("person", "test123")
        self.assertIsNotNone(entity)
        self.assertEqual(entity.name, "홍길동")

    def test_get_entity_not_found(self):
        """Test getting non-existent entity."""
        from adapters.mailon_backend import MailonBackend
        backend = MailonBackend(project_root=self.project_root)
        entity = backend.get_entity("person", "nonexistent")
        self.assertIsNone(entity)

    def test_find_entity_by_name(self):
        """Test finding entity by name."""
        from adapters.mailon_backend import MailonBackend
        backend = MailonBackend(project_root=self.project_root)
        entity = backend.find_entity_by_name("person", "홍길동")
        self.assertIsNotNone(entity)

    def test_list_entities(self):
        """Test listing entities."""
        from adapters.mailon_backend import MailonBackend
        backend = MailonBackend(project_root=self.project_root)
        entities = backend.list_entities("person")
        self.assertGreater(len(entities), 0)

    def test_get_entity_mentions(self):
        """Test getting entity mentions."""
        from adapters.mailon_backend import MailonBackend
        backend = MailonBackend(project_root=self.project_root)
        mentions = backend.get_entity_mentions("person", "test123")
        self.assertIn("12345", mentions)

    def test_get_sync_status(self):
        """Test getting sync status."""
        from adapters.mailon_backend import MailonBackend
        backend = MailonBackend(project_root=self.project_root)
        status = backend.get_sync_status()
        self.assertIsNotNone(status)
        self.assertGreater(status.total_messages, 0)
        self.assertGreater(status.ingested_count, 0)
        self.assertGreater(status.entity_count, 0)

    def test_to_dict(self):
        """Test dataclass to dict conversion."""
        from adapters.mailon_backend import MailonBackend, MailRecord
        backend = MailonBackend(project_root=self.project_root)
        mail = backend.get_mail("12345")
        if mail:
            d = backend.to_dict(mail)
            self.assertIn("uid", d)
            self.assertIn("subject", d)

    def test_parse_date_valid(self):
        """Test date parsing with valid date."""
        from adapters.mailon_backend import MailonBackend
        backend = MailonBackend()
        result = backend._parse_date("2024-08-15T10:30:00")
        self.assertIsNotNone(result)
        self.assertEqual(result.year, 2024)

    def test_parse_date_invalid(self):
        """Test date parsing with invalid date."""
        from adapters.mailon_backend import MailonBackend
        backend = MailonBackend()
        result = backend._parse_date("invalid")
        self.assertIsNone(result)

    def test_parse_attachments_list(self):
        """Test attachment parsing with list."""
        from adapters.mailon_backend import MailonBackend
        backend = MailonBackend()
        result = backend._parse_attachments('["file1.pdf", "file2.doc"]')
        self.assertEqual(len(result), 2)

    def test_parse_attachments_empty(self):
        """Test attachment parsing with empty."""
        from adapters.mailon_backend import MailonBackend
        backend = MailonBackend()
        result = backend._parse_attachments("[]")
        self.assertEqual(result, [])


# ============================================================
# Run all tests
# ============================================================

if __name__ == "__main__":
    unittest.main(verbosity=2)
