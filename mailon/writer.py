"""Write each email as a Markdown file with YAML front-matter.

Layout:
    data/mails/YYYY/MM/YYYY-MM-DD_<slug>_<uid>.md
    data/attachments/<uid>/<filename>

The Markdown front-matter is YAML so Obsidian/Logseq/etc can index it.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable


log = logging.getLogger(__name__)


_SLUG_BANNED = re.compile(r'[\\/:*?"<>|\r\n\t]+')
_MULTI_UNDERSCORE = re.compile(r"_+")


def slugify(text: str, max_len: int = 60) -> str:
    """Filesystem-safe slug. Keeps Korean characters."""
    s = _SLUG_BANNED.sub("_", text or "").strip()
    s = _MULTI_UNDERSCORE.sub("_", s).strip("_")
    if len(s) > max_len:
        s = s[:max_len].rstrip("_")
    return s or "untitled"


@dataclass
class Attachment:
    filename: str
    local_path: Path  # absolute or project-relative
    size: int | None = None


@dataclass
class Mail:
    uid: str
    folder: str
    subject: str
    sender: str
    to: str = ""
    cc: str = ""
    date: datetime | None = None
    body_text: str = ""       # plain text view (preferred for Markdown body)
    body_html: str = ""       # optional raw HTML for reference
    attachments: list[Attachment] = field(default_factory=list)
    collected_at: datetime = field(default_factory=datetime.now)


def _yaml_escape(val: str) -> str:
    """Minimal YAML scalar escape for double-quoted strings."""
    if val is None:
        return '""'
    return '"' + str(val).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _yaml_list(items: Iterable[str]) -> str:
    items = list(items)
    if not items:
        return "[]"
    return "\n" + "\n".join(f"  - {_yaml_escape(x)}" for x in items)


def build_markdown(mail: Mail, project_root: Path, md_path: Path) -> str:
    """Render a Mail as a complete Markdown document string."""
    date_iso = mail.date.isoformat() if mail.date else ""
    collected_iso = mail.collected_at.isoformat()

    # Attachments: relative path from the .md file to each attachment
    att_entries: list[str] = []
    for a in mail.attachments:
        try:
            rel = Path(a.local_path).resolve().relative_to(project_root.resolve())
            # Path relative from md file location to attachment
            rel_from_md = Path(
                *[".." for _ in md_path.resolve().parent.relative_to(project_root.resolve()).parts]
            ) / rel
            href = rel_from_md.as_posix()
        except ValueError:
            href = str(a.local_path)
        size = f" ({a.size} bytes)" if a.size else ""
        att_entries.append(f"- [{a.filename}]({href}){size}")

    # Front matter
    lines = [
        "---",
        f"uid: {_yaml_escape(mail.uid)}",
        f"folder: {_yaml_escape(mail.folder)}",
        f"subject: {_yaml_escape(mail.subject)}",
        f"from: {_yaml_escape(mail.sender)}",
        f"to: {_yaml_escape(mail.to)}",
        f"cc: {_yaml_escape(mail.cc)}",
        f"date: {_yaml_escape(date_iso)}",
        f"collected_at: {_yaml_escape(collected_iso)}",
        f"attachments:{_yaml_list(a.filename for a in mail.attachments)}",
        "---",
        "",
        f"# {mail.subject or '(제목 없음)'}",
        "",
        f"**From**: {mail.sender}  ",
        f"**Date**: {date_iso}  ",
    ]
    if mail.to:
        lines.append(f"**To**: {mail.to}  ")
    if mail.cc:
        lines.append(f"**Cc**: {mail.cc}  ")
    lines.append("")

    if att_entries:
        lines.append("## Attachments")
        lines.append("")
        lines.extend(att_entries)
        lines.append("")

    lines.append("## Body")
    lines.append("")
    lines.append(mail.body_text.strip() or "(빈 본문)")
    lines.append("")

    return "\n".join(lines)


def path_for(mail: Mail, mails_dir: Path) -> Path:
    """Decide on-disk location for an email's Markdown file."""
    dt = mail.date or mail.collected_at
    yyyy = f"{dt.year:04d}"
    mm = f"{dt.month:02d}"
    dd = f"{dt.day:02d}"
    slug = slugify(mail.subject or "untitled")
    filename = f"{yyyy}-{mm}-{dd}_{slug}_{mail.uid}.md"
    folder = mails_dir / yyyy / mm
    folder.mkdir(parents=True, exist_ok=True)
    return folder / filename


def write_mail(mail: Mail, mails_dir: Path, project_root: Path) -> Path:
    """Write a Markdown file; return the path."""
    md_path = path_for(mail, mails_dir)
    content = build_markdown(mail, project_root=project_root, md_path=md_path)
    md_path.write_text(content, encoding="utf-8")
    log.info("wrote markdown: %s", md_path.relative_to(project_root))
    return md_path
