"""Sync mail content to LLM Wiki (Claudesidian).

Copies ingested mail from staging/ to the LLM Wiki vault.
Supports both incremental sync and full mirror mode.

Usage:
    python -m scripts.sync_to_wiki
    python -m scripts.sync_to_wiki --dry-run
    python -m scripts.sync_to_wiki --full-mirror
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STAGING_DIR = PROJECT_ROOT / "staging"
STAGING_MAIL_DIR = STAGING_DIR / "mail"
STAGING_WIKI_DIR = STAGING_DIR / "wiki"

# LLM Wiki target paths
WIKI_ROOT = Path.home() / "claude_projects" / "my-vault"
WIKI_MAIL_TARGET = WIKI_ROOT / "02_Areas" / "MailON"
WIKI_PEOPLE_TARGET = WIKI_ROOT / "02_Areas" / "MailON-People"
WIKI_PROJECTS_TARGET = WIKI_ROOT / "02_Areas" / "MailON-Projects"


def ensure_wiki_exists() -> bool:
    """Check if Wiki vault exists."""
    if not WIKI_ROOT.exists():
        log.error("LLM Wiki not found at: %s", WIKI_ROOT)
        return False
    if not (WIKI_ROOT / ".obsidian").exists():
        log.warning("Not an Obsidian vault: %s", WIKI_ROOT)
    return True


def sync_with_robocopy(source: Path, target: Path, dry_run: bool = False, mirror: bool = False) -> int:
    """
    Use robocopy for Windows sync.

    Returns: 0-7 = success levels, 8+ = error
    """
    if not source.exists():
        log.warning("Source does not exist: %s", source)
        return 0

    target.mkdir(parents=True, exist_ok=True)

    cmd = [
        "robocopy",
        str(source),
        str(target),
        "/E",       # Copy subdirectories including empty
        "/NFL",     # No file list
        "/NDL",     # No directory list
        "/NJH",     # No job header
        "/NJS",     # No job summary
        "/NP",      # No progress
        "/R:1",     # 1 retry
        "/W:1",     # 1 second wait
    ]

    if mirror:
        cmd.append("/MIR")  # Mirror (delete extras in target)

    if dry_run:
        cmd.append("/L")    # List only, no copy

    log.info("Running: %s", " ".join(cmd[:5]) + " ...")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        # robocopy exit codes: 0-7 are success, 8+ are errors
        if result.returncode >= 8:
            log.error("robocopy failed with code %d:\n%s", result.returncode, result.stderr)
            return result.returncode
        return 0
    except FileNotFoundError:
        log.error("robocopy not found - are you on Windows?")
        return 1


def sync_with_shutil(source: Path, target: Path, dry_run: bool = False, mirror: bool = False) -> int:
    """
    Fallback sync using Python shutil.

    For non-Windows or when robocopy is unavailable.
    """
    if not source.exists():
        log.warning("Source does not exist: %s", source)
        return 0

    target.mkdir(parents=True, exist_ok=True)

    copied = 0
    skipped = 0

    for src_file in source.rglob("*"):
        if src_file.is_dir():
            continue

        rel_path = src_file.relative_to(source)
        dst_file = target / rel_path

        # Check if copy needed
        if dst_file.exists():
            # Compare by size and mtime
            src_stat = src_file.stat()
            dst_stat = dst_file.stat()
            if src_stat.st_size == dst_stat.st_size and src_stat.st_mtime <= dst_stat.st_mtime:
                skipped += 1
                continue

        if dry_run:
            log.debug("Would copy: %s", rel_path)
            copied += 1
            continue

        dst_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, dst_file)
        copied += 1

    # Handle mirror mode (delete extras)
    if mirror and not dry_run:
        for dst_file in target.rglob("*"):
            if dst_file.is_dir():
                continue
            rel_path = dst_file.relative_to(target)
            src_file = source / rel_path
            if not src_file.exists():
                log.debug("Removing: %s", rel_path)
                dst_file.unlink()

    log.info("Copied: %d, Skipped: %d", copied, skipped)
    return 0


def sync_directory(source: Path, target: Path, dry_run: bool = False, mirror: bool = False) -> int:
    """Sync a directory using best available method."""
    if sys.platform == "win32":
        return sync_with_robocopy(source, target, dry_run, mirror)
    else:
        return sync_with_shutil(source, target, dry_run, mirror)


def create_wiki_index(target: Path, title: str, dry_run: bool = False) -> None:
    """Create an index file for the Wiki folder."""
    index_path = target / "README.md"

    if index_path.exists():
        return

    content = f"""---
title: {title}
type: index
source: mailon-backup
updated: {datetime.now().isoformat()}
---

# {title}

이 폴더는 mailon-backup에서 자동 동기화된 메일입니다.

## 폴더 구조

- 연도/월별로 정리됨
- YAML frontmatter 포함
- Obsidian에서 검색 가능

## 동기화 정보

- 소스: `mailon-backup/staging/`
- 대상: `my-vault/02_Areas/MailON/`
- 방식: 증분 동기화

## 관련 링크

- [[02_Areas/README|Areas 목록]]
"""

    if not dry_run:
        target.mkdir(parents=True, exist_ok=True)
        index_path.write_text(content, encoding="utf-8")
        log.info("Created index: %s", index_path)


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Sync mail to LLM Wiki")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done")
    parser.add_argument("--full-mirror", action="store_true", help="Mirror mode (delete extras)")
    parser.add_argument("--include-entities", action="store_true", help="Also sync wiki entities")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    log.info("Starting Wiki sync...")

    if args.dry_run:
        log.info("DRY RUN - no files will be modified")

    # Check Wiki exists
    if not ensure_wiki_exists():
        return 1

    # Sync mail
    log.info("Syncing mail to: %s", WIKI_MAIL_TARGET)
    ret = sync_directory(
        STAGING_MAIL_DIR,
        WIKI_MAIL_TARGET,
        dry_run=args.dry_run,
        mirror=args.full_mirror,
    )
    if ret != 0:
        return ret

    # Create index
    create_wiki_index(WIKI_MAIL_TARGET, "MailON 메일", dry_run=args.dry_run)

    # Optionally sync entities
    if args.include_entities:
        log.info("Syncing people to: %s", WIKI_PEOPLE_TARGET)
        sync_directory(
            STAGING_WIKI_DIR / "people",
            WIKI_PEOPLE_TARGET,
            dry_run=args.dry_run,
            mirror=args.full_mirror,
        )
        create_wiki_index(WIKI_PEOPLE_TARGET, "MailON 연락처", dry_run=args.dry_run)

        log.info("Syncing projects to: %s", WIKI_PROJECTS_TARGET)
        sync_directory(
            STAGING_WIKI_DIR / "projects",
            WIKI_PROJECTS_TARGET,
            dry_run=args.dry_run,
            mirror=args.full_mirror,
        )
        create_wiki_index(WIKI_PROJECTS_TARGET, "MailON 프로젝트", dry_run=args.dry_run)

    log.info("Wiki sync complete!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
