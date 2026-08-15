"""Daily briefing generator for mail activity.

Generates a daily summary including:
- New mails received in the last 24 hours
- Key entities (people, projects) mentioned
- Action items detected
- Upcoming meetings referenced
- Attachments received

Output: staging/briefs/YYYY-MM-DD.md

Usage:
    python -m scripts.daily_brief
    python -m scripts.daily_brief --dry-run
    python -m scripts.daily_brief --date 2024-08-15
    python -m scripts.daily_brief --days 7
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import sys
from collections import defaultdict
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
BRIEFS_DIR = STAGING_DIR / "briefs"
INGEST_DB = DATA_DIR / "ingest.db"
ENTITY_DB = DATA_DIR / "entities.db"


@dataclass
class MailSummary:
    """Summary of a single mail."""
    uid: str
    subject: str
    sender: str
    date: datetime
    has_attachments: bool
    attachment_count: int
    is_reply: bool
    is_forward: bool
    priority: str  # normal, high, low
    word_count: int


@dataclass
class DailyBrief:
    """Daily briefing data."""
    date: datetime
    mail_count: int
    mails: list[MailSummary]
    top_senders: list[tuple[str, int]]
    people_mentioned: list[tuple[str, int]]
    projects_mentioned: list[tuple[str, int]]
    action_items: list[str]
    attachments: list[tuple[str, str, str]]  # (filename, mail_subject, mail_uid)
    meetings: list[tuple[str, str]]  # (meeting_title, date)


# ---- Action Item Detection ----

ACTION_PATTERNS = [
    re.compile(r'(?:해주|부탁|요청|확인|검토|회신|보내|제출|작성)(?:세요|해\s*주세요|드립니다|바랍니다)', re.IGNORECASE),
    re.compile(r'(?:please|kindly)\s+(?:review|check|send|submit|respond|reply)', re.IGNORECASE),
    re.compile(r'(?:까지|전에|이내에)\s*(?:제출|회신|완료)', re.IGNORECASE),
    re.compile(r'(?:urgent|긴급|중요|asap)', re.IGNORECASE),
    re.compile(r'(?:due|deadline|마감).*(?:\d{4}[-./]\d{1,2}[-./]\d{1,2}|\d{1,2}월\s*\d{1,2}일)', re.IGNORECASE),
]


def extract_action_items(content: str, subject: str) -> list[str]:
    """Extract potential action items from mail content."""
    items = []

    for pattern in ACTION_PATTERNS:
        matches = pattern.finditer(content)
        for match in matches:
            # Extract context around match
            start = max(0, match.start() - 30)
            end = min(len(content), match.end() + 50)
            context = content[start:end].replace('\n', ' ').strip()
            if context and len(context) > 10:
                items.append(f"[{subject[:30]}...] {context}")

    return items


# ---- Meeting Detection ----

MEETING_PATTERNS = [
    re.compile(r'(?:회의|미팅|간담회|세미나).*?(\d{4}[-./]\d{1,2}[-./]\d{1,2}|\d{1,2}월\s*\d{1,2}일)', re.IGNORECASE),
    re.compile(r'(\d{4}[-./]\d{1,2}[-./]\d{1,2}|\d{1,2}월\s*\d{1,2}일).*?(?:회의|미팅|간담회|세미나)', re.IGNORECASE),
]


def extract_meetings(content: str) -> list[tuple[str, str]]:
    """Extract meeting references from content."""
    meetings = []

    for pattern in MEETING_PATTERNS:
        matches = pattern.finditer(content)
        for match in matches:
            full = match.group(0)[:60]
            date = match.group(1) if match.lastindex else ""
            meetings.append((full, date))

    return meetings


# ---- Mail Loading ----

def load_mails_for_period(start: datetime, end: datetime) -> list[dict]:
    """Load mails from database within date range."""
    if not INGEST_DB.exists():
        return []

    conn = sqlite3.connect(INGEST_DB)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        """SELECT file_path, staging_path, uid, subject, sender, recv_date
           FROM ingested_files
           WHERE recv_date >= ? AND recv_date < ?
           ORDER BY recv_date DESC""",
        (start.isoformat(), end.isoformat())
    ).fetchall()

    conn.close()
    return [dict(r) for r in rows]


def parse_mail_content(path: Path) -> dict:
    """Parse staging mail file for briefing data."""
    try:
        content = path.read_text(encoding="utf-8")
    except Exception as e:
        log.warning("Failed to read %s: %s", path, e)
        return {}

    # Extract frontmatter
    data = {}
    if content.startswith("---"):
        lines = content.split("\n")
        end_idx = None
        for i, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                end_idx = i
                break

        if end_idx:
            frontmatter = "\n".join(lines[1:end_idx])
            body = "\n".join(lines[end_idx + 1:])

            # Simple YAML parsing
            for line in frontmatter.split("\n"):
                if ":" in line:
                    key, val = line.split(":", 1)
                    data[key.strip()] = val.strip()

            data["body"] = body
    else:
        data["body"] = content

    return data


def generate_brief(target_date: datetime) -> DailyBrief:
    """Generate daily brief for a specific date."""
    start = datetime(target_date.year, target_date.month, target_date.day)
    end = start + timedelta(days=1)

    mails_data = load_mails_for_period(start, end)
    log.info("Found %d mails for %s", len(mails_data), target_date.date())

    mails: list[MailSummary] = []
    sender_counts: dict[str, int] = defaultdict(int)
    people_counts: dict[str, int] = defaultdict(int)
    project_counts: dict[str, int] = defaultdict(int)
    all_action_items: list[str] = []
    all_attachments: list[tuple[str, str, str]] = []
    all_meetings: list[tuple[str, str]] = []

    for mail_meta in mails_data:
        staging_path = PROJECT_ROOT / mail_meta["staging_path"]
        mail_content = parse_mail_content(staging_path)

        subject = mail_meta.get("subject", "") or mail_content.get("subject", "")
        sender = mail_meta.get("sender", "") or mail_content.get("from", "")
        body = mail_content.get("body", "")

        # Parse date
        date_str = mail_meta.get("recv_date", "") or mail_content.get("date", "")
        try:
            mail_date = datetime.fromisoformat(date_str.replace('"', '').strip())
        except (ValueError, TypeError):
            mail_date = target_date

        # Detect mail type
        is_reply = subject.lower().startswith(("re:", "답장:", "회신:"))
        is_forward = subject.lower().startswith(("fwd:", "fw:", "전달:"))

        # Attachments
        attachments_str = mail_content.get("attachments", "[]")
        try:
            if attachments_str.startswith("["):
                attachments = eval(attachments_str)  # Safe for known format
            else:
                attachments = []
        except Exception:
            attachments = []

        for att in attachments:
            all_attachments.append((att, subject[:40], mail_meta.get("uid", "")))

        # Word count
        word_count = len(body.split())

        # Track sender
        sender_counts[sender] += 1

        # Extract action items
        action_items = extract_action_items(body, subject)
        all_action_items.extend(action_items)

        # Extract meetings
        meetings = extract_meetings(body)
        all_meetings.extend(meetings)

        # Create summary
        mails.append(MailSummary(
            uid=mail_meta.get("uid", ""),
            subject=subject,
            sender=sender,
            date=mail_date,
            has_attachments=len(attachments) > 0,
            attachment_count=len(attachments),
            is_reply=is_reply,
            is_forward=is_forward,
            priority="normal",
            word_count=word_count,
        ))

    # Sort top senders
    top_senders = sorted(sender_counts.items(), key=lambda x: x[1], reverse=True)[:10]

    return DailyBrief(
        date=target_date,
        mail_count=len(mails),
        mails=mails,
        top_senders=top_senders,
        people_mentioned=list(people_counts.items())[:10],
        projects_mentioned=list(project_counts.items())[:10],
        action_items=all_action_items[:20],
        attachments=all_attachments,
        meetings=all_meetings,
    )


def format_brief_markdown(brief: DailyBrief) -> str:
    """Format daily brief as markdown."""
    lines = [
        "---",
        f"type: daily_brief",
        f"date: {brief.date.date().isoformat()}",
        f"mail_count: {brief.mail_count}",
        f"generated: {datetime.now().isoformat()}",
        "---",
        "",
        f"# Daily Brief: {brief.date.strftime('%Y-%m-%d (%A)')}",
        "",
        "## Summary",
        "",
        f"- **Total Mails**: {brief.mail_count}",
        f"- **Attachments**: {len(brief.attachments)}",
        f"- **Action Items**: {len(brief.action_items)}",
        f"- **Meeting References**: {len(brief.meetings)}",
        "",
    ]

    # Top senders
    if brief.top_senders:
        lines.append("## Top Senders")
        lines.append("")
        for sender, count in brief.top_senders[:5]:
            lines.append(f"- {sender}: {count}건")
        lines.append("")

    # Action items
    if brief.action_items:
        lines.append("## Action Items")
        lines.append("")
        for item in brief.action_items[:10]:
            lines.append(f"- {item}")
        lines.append("")

    # Meetings
    if brief.meetings:
        lines.append("## Meeting References")
        lines.append("")
        seen = set()
        for title, date in brief.meetings:
            if title not in seen:
                lines.append(f"- {title}")
                seen.add(title)
        lines.append("")

    # Attachments
    if brief.attachments:
        lines.append("## Attachments Received")
        lines.append("")
        for filename, subject, uid in brief.attachments[:20]:
            lines.append(f"- `{filename}` from [{subject}...]")
        lines.append("")

    # Mail list
    lines.append("## All Mails")
    lines.append("")
    for mail in brief.mails:
        flags = []
        if mail.is_reply:
            flags.append("RE")
        if mail.is_forward:
            flags.append("FW")
        if mail.has_attachments:
            flags.append(f"{mail.attachment_count} att")

        flag_str = f" [{', '.join(flags)}]" if flags else ""
        lines.append(f"- **{mail.subject}**{flag_str}")
        lines.append(f"  - From: {mail.sender}")
        lines.append(f"  - Date: {mail.date.strftime('%H:%M')}")
    lines.append("")

    return "\n".join(lines)


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Generate daily mail briefing")
    parser.add_argument("--dry-run", action="store_true", help="Don't write output file")
    parser.add_argument("--date", help="Target date (YYYY-MM-DD), default: today")
    parser.add_argument("--days", type=int, default=1, help="Number of days to generate")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    # Parse target date
    if args.date:
        try:
            target = datetime.strptime(args.date, "%Y-%m-%d")
        except ValueError:
            log.error("Invalid date format. Use YYYY-MM-DD")
            return 1
    else:
        target = datetime.now()

    log.info("Generating daily brief(s)...")

    BRIEFS_DIR.mkdir(parents=True, exist_ok=True)

    for i in range(args.days):
        current = target - timedelta(days=i)
        log.info("Processing: %s", current.date())

        brief = generate_brief(current)

        if args.json:
            output = {
                "date": brief.date.isoformat(),
                "mail_count": brief.mail_count,
                "top_senders": brief.top_senders,
                "action_items": brief.action_items,
                "attachments_count": len(brief.attachments),
                "meetings_count": len(brief.meetings),
            }
            print(json.dumps(output, ensure_ascii=False, indent=2))
        else:
            markdown = format_brief_markdown(brief)

            if args.dry_run:
                print(markdown)
            else:
                output_path = BRIEFS_DIR / f"{current.date().isoformat()}.md"
                output_path.write_text(markdown, encoding="utf-8")
                log.info("Wrote: %s", output_path)

    log.info("Daily brief generation complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
