"""Natural language search for mail content.

Features:
- Time range parsing (지난 3년, 최근 한 달, 2024년)
- Person name extraction (이경일 박사, 홍길동)
- Intent classification (find, summarize, action_needed)
- Full-text search with relevance scoring
- Entity-aware search (cross-reference with wiki)

Usage:
    python -m scripts.search_mail "이경일 박사 보드 제작 관련 메일"
    python -m scripts.search_mail "지난 3개월 회의 일정"
    python -m scripts.search_mail --intent summarize "프로젝트 진행 상황"
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
STAGING_DIR = PROJECT_ROOT / "staging"
STAGING_MAIL_DIR = STAGING_DIR / "mail"
INGEST_DB = DATA_DIR / "ingest.db"
ENTITY_DB = DATA_DIR / "entities.db"


@dataclass
class ParsedQuery:
    """Parsed natural language query."""
    original: str
    keywords: list[str] = field(default_factory=list)
    person_names: list[str] = field(default_factory=list)
    time_start: datetime | None = None
    time_end: datetime | None = None
    intent: str = "find"  # find, summarize, action_needed


@dataclass
class SearchResult:
    """A single search result."""
    uid: str
    subject: str
    sender: str
    date: str
    relevance: float
    snippet: str
    file_path: str
    matched_terms: list[str] = field(default_factory=list)


# ---- Time Range Parsing ----

TIME_PATTERNS = [
    # Korean relative time
    (re.compile(r'지난\s*(\d+)\s*년'), lambda m: ("years", int(m.group(1)))),
    (re.compile(r'지난\s*(\d+)\s*개월'), lambda m: ("months", int(m.group(1)))),
    (re.compile(r'지난\s*(\d+)\s*주'), lambda m: ("weeks", int(m.group(1)))),
    (re.compile(r'지난\s*(\d+)\s*일'), lambda m: ("days", int(m.group(1)))),
    (re.compile(r'최근\s*(\d+)\s*년'), lambda m: ("years", int(m.group(1)))),
    (re.compile(r'최근\s*(\d+)\s*개월'), lambda m: ("months", int(m.group(1)))),
    (re.compile(r'최근\s*(\d+)\s*주'), lambda m: ("weeks", int(m.group(1)))),
    (re.compile(r'최근\s*한\s*달'), lambda m: ("months", 1)),
    (re.compile(r'최근\s*일주일'), lambda m: ("weeks", 1)),
    (re.compile(r'오늘'), lambda m: ("days", 0)),
    (re.compile(r'어제'), lambda m: ("days", 1)),
    (re.compile(r'이번\s*주'), lambda m: ("weeks", 0)),
    (re.compile(r'이번\s*달'), lambda m: ("months", 0)),
    (re.compile(r'올해'), lambda m: ("year_current", 0)),
    (re.compile(r'작년'), lambda m: ("year_prev", 0)),
    # Specific year
    (re.compile(r'(\d{4})년'), lambda m: ("year_exact", int(m.group(1)))),
    # Month specification
    (re.compile(r'(\d{1,2})월'), lambda m: ("month_exact", int(m.group(1)))),
]


def parse_time_range(query: str) -> tuple[datetime | None, datetime | None, str]:
    """Parse time range from query, return (start, end, cleaned_query)."""
    now = datetime.now()
    start = None
    end = now
    cleaned = query

    for pattern, extractor in TIME_PATTERNS:
        match = pattern.search(query)
        if match:
            unit, value = extractor(match)
            cleaned = pattern.sub('', cleaned).strip()

            if unit == "years":
                start = now - timedelta(days=365 * value)
            elif unit == "months":
                start = now - timedelta(days=30 * value)
            elif unit == "weeks":
                start = now - timedelta(weeks=value)
            elif unit == "days":
                start = now - timedelta(days=value)
            elif unit == "year_current":
                start = datetime(now.year, 1, 1)
            elif unit == "year_prev":
                start = datetime(now.year - 1, 1, 1)
                end = datetime(now.year - 1, 12, 31)
            elif unit == "year_exact":
                start = datetime(value, 1, 1)
                end = datetime(value, 12, 31)
            elif unit == "month_exact":
                start = datetime(now.year, value, 1)
                if value == 12:
                    end = datetime(now.year + 1, 1, 1) - timedelta(days=1)
                else:
                    end = datetime(now.year, value + 1, 1) - timedelta(days=1)
            break

    return start, end, cleaned


# ---- Person Name Extraction ----

KOREAN_NAME_PATTERN = re.compile(r'([가-힣]{2,4})(?:\s*(?:박사|교수|선생님|님|과장|대리|부장|팀장|소장|원장|연구원|주임|담당))?')
TITLE_SUFFIXES = re.compile(r'(?:박사|교수|선생님|님|과장|대리|부장|팀장|소장|원장|연구원|주임|담당)$')


def extract_person_names(query: str) -> tuple[list[str], str]:
    """Extract person names from query, return (names, cleaned_query)."""
    names = []
    cleaned = query

    for match in KOREAN_NAME_PATTERN.finditer(query):
        full_match = match.group(0)
        name = match.group(1)

        # Skip common non-name words
        if name in ("안녕", "감사", "회신", "확인", "검토", "참고", "수정", "완료", "관련", "내용"):
            continue

        names.append(name)
        cleaned = cleaned.replace(full_match, '').strip()

    return names, cleaned


# ---- Intent Classification ----

INTENT_PATTERNS = {
    "summarize": [
        re.compile(r'요약|정리|개요|브리프|브리핑'),
        re.compile(r'summarize|summary|brief|overview'),
    ],
    "action_needed": [
        re.compile(r'해야\s*할|할\s*일|액션|todo|action|urgent|긴급'),
        re.compile(r'회신\s*필요|답장\s*필요|미완료|pending'),
    ],
    "find": [
        re.compile(r'찾|검색|search|find|where|어디'),
    ],
}


def classify_intent(query: str) -> str:
    """Classify query intent."""
    for intent, patterns in INTENT_PATTERNS.items():
        for pattern in patterns:
            if pattern.search(query):
                return intent
    return "find"


# ---- Query Parsing ----

def parse_query(query: str) -> ParsedQuery:
    """Parse a natural language query into structured form."""
    original = query

    # Extract time range
    time_start, time_end, query = parse_time_range(query)

    # Extract person names
    person_names, query = extract_person_names(query)

    # Classify intent
    intent = classify_intent(query)

    # Extract remaining keywords
    # Remove common stop words
    stop_words = {"메일", "이메일", "email", "mail", "관련", "에", "대해", "의", "를", "은", "는", "이", "가"}
    words = re.findall(r'[\w가-힣]+', query)
    keywords = [w for w in words if w.lower() not in stop_words and len(w) > 1]

    return ParsedQuery(
        original=original,
        keywords=keywords,
        person_names=person_names,
        time_start=time_start,
        time_end=time_end,
        intent=intent,
    )


# ---- Search Implementation ----

def load_ingested_mails() -> list[dict]:
    """Load all ingested mail metadata from database."""
    if not INGEST_DB.exists():
        return []

    conn = sqlite3.connect(INGEST_DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT file_path, staging_path, uid, subject, sender, recv_date FROM ingested_files ORDER BY recv_date DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def compute_relevance(content: str, parsed: ParsedQuery) -> tuple[float, list[str], str]:
    """Compute relevance score and extract snippet."""
    score = 0.0
    matched = []
    snippet = ""

    content_lower = content.lower()

    # Score keywords
    for keyword in parsed.keywords:
        if keyword.lower() in content_lower:
            score += 1.0
            matched.append(keyword)
            # Extract snippet around keyword
            idx = content_lower.find(keyword.lower())
            if idx >= 0:
                start = max(0, idx - 50)
                end = min(len(content), idx + len(keyword) + 100)
                snippet = content[start:end].replace('\n', ' ')

    # Score person names (higher weight)
    for name in parsed.person_names:
        if name in content:
            score += 2.0
            matched.append(name)

    return score, matched, snippet


def search(parsed: ParsedQuery, limit: int = 20) -> list[SearchResult]:
    """Execute search based on parsed query."""
    results = []
    mails = load_ingested_mails()

    for mail in mails:
        # Time filter
        if parsed.time_start or parsed.time_end:
            try:
                mail_date = datetime.fromisoformat(mail["recv_date"].replace('"', '').strip())
                if parsed.time_start and mail_date < parsed.time_start:
                    continue
                if parsed.time_end and mail_date > parsed.time_end:
                    continue
            except (ValueError, TypeError):
                pass

        # Load content
        staging_path = PROJECT_ROOT / mail["staging_path"]
        if not staging_path.exists():
            continue

        try:
            content = staging_path.read_text(encoding="utf-8")
        except Exception:
            continue

        # Compute relevance
        relevance, matched, snippet = compute_relevance(content, parsed)

        if relevance > 0 or not (parsed.keywords or parsed.person_names):
            results.append(SearchResult(
                uid=mail["uid"] or "",
                subject=mail["subject"] or "",
                sender=mail["sender"] or "",
                date=mail["recv_date"] or "",
                relevance=relevance,
                snippet=snippet[:200],
                file_path=mail["staging_path"],
                matched_terms=matched,
            ))

    # Sort by relevance
    results.sort(key=lambda r: r.relevance, reverse=True)
    return results[:limit]


def format_results(results: list[SearchResult], parsed: ParsedQuery) -> str:
    """Format search results for display."""
    lines = []

    # Header
    lines.append(f"검색 쿼리: {parsed.original}")
    lines.append(f"의도: {parsed.intent}")
    if parsed.time_start:
        lines.append(f"기간: {parsed.time_start.date()} ~ {parsed.time_end.date() if parsed.time_end else '현재'}")
    if parsed.person_names:
        lines.append(f"인물: {', '.join(parsed.person_names)}")
    if parsed.keywords:
        lines.append(f"키워드: {', '.join(parsed.keywords)}")
    lines.append("")
    lines.append(f"검색 결과: {len(results)}건")
    lines.append("=" * 60)

    for i, result in enumerate(results, 1):
        lines.append("")
        lines.append(f"[{i}] {result.subject}")
        lines.append(f"    From: {result.sender}")
        lines.append(f"    Date: {result.date}")
        lines.append(f"    Relevance: {result.relevance:.1f}")
        if result.matched_terms:
            lines.append(f"    Matched: {', '.join(result.matched_terms)}")
        if result.snippet:
            lines.append(f"    Snippet: {result.snippet}...")

    return "\n".join(lines)


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Natural language mail search")
    parser.add_argument("query", nargs="?", help="Search query")
    parser.add_argument("--intent", choices=["find", "summarize", "action_needed"], help="Override intent")
    parser.add_argument("--limit", type=int, default=20, help="Maximum results")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    if not args.query:
        # Interactive mode
        print("메일 검색 (종료: q)")
        print()
        while True:
            try:
                query = input("검색> ").strip()
            except (KeyboardInterrupt, EOFError):
                break
            if query.lower() in ("q", "quit", "exit"):
                break
            if not query:
                continue

            parsed = parse_query(query)
            if args.intent:
                parsed.intent = args.intent

            results = search(parsed, limit=args.limit)
            print(format_results(results, parsed))
            print()
        return 0

    # Single query mode
    parsed = parse_query(args.query)
    if args.intent:
        parsed.intent = args.intent

    log.debug("Parsed query: %s", parsed)

    results = search(parsed, limit=args.limit)

    if args.json:
        output = {
            "query": {
                "original": parsed.original,
                "keywords": parsed.keywords,
                "person_names": parsed.person_names,
                "time_start": parsed.time_start.isoformat() if parsed.time_start else None,
                "time_end": parsed.time_end.isoformat() if parsed.time_end else None,
                "intent": parsed.intent,
            },
            "results": [
                {
                    "uid": r.uid,
                    "subject": r.subject,
                    "sender": r.sender,
                    "date": r.date,
                    "relevance": r.relevance,
                    "snippet": r.snippet,
                    "matched_terms": r.matched_terms,
                }
                for r in results
            ],
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        print(format_results(results, parsed))

    return 0


if __name__ == "__main__":
    sys.exit(main())
