"""Post-sync hook that triggers after mailon sync completes.

This hook:
1. Detects when new mails are synced
2. Triggers incremental ingestion to staging/
3. Optionally triggers entity extraction
4. Logs the operation for audit

Usage:
    # Called automatically after mailon sync
    python -m hooks.post_sync

    # Force trigger (useful for testing)
    python -m hooks.post_sync --force
"""
from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
STATE_DB = DATA_DIR / "state.db"
STAGING_DIR = PROJECT_ROOT / "staging"
HOOKS_LOG = PROJECT_ROOT / "logs" / "hooks.log"


def setup_logging() -> None:
    """Configure logging for hook execution."""
    HOOKS_LOG.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(HOOKS_LOG, encoding="utf-8"),
        ],
    )


def get_last_run_info() -> dict | None:
    """Get info about the most recent mailon sync run."""
    import sqlite3

    if not STATE_DB.exists():
        return None

    try:
        conn = sqlite3.connect(STATE_DB)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """SELECT run_id, started_at, finished_at, status, new_mails, error
               FROM runs ORDER BY run_id DESC LIMIT 1"""
        ).fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception as e:
        log.error("Failed to read runs table: %s", e)
        return None


def should_trigger_ingest(last_run: dict | None, force: bool = False) -> bool:
    """Determine if ingestion should be triggered."""
    if force:
        log.info("Forced trigger requested")
        return True

    if not last_run:
        log.warning("No sync runs found in state.db")
        return False

    if last_run.get("status") != "ok":
        log.warning("Last run status is not 'ok': %s", last_run.get("status"))
        return False

    new_mails = last_run.get("new_mails", 0)
    if new_mails > 0:
        log.info("Last run synced %d new mails, triggering ingestion", new_mails)
        return True

    log.info("No new mails in last run, skipping ingestion")
    return False


def run_ingest(dry_run: bool = False) -> int:
    """Execute the mail ingestion script."""
    cmd = [sys.executable, "-m", "scripts.ingest_mail"]
    if dry_run:
        cmd.append("--dry-run")

    log.info("Running: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=300,  # 5 minute timeout
        )
        if result.returncode != 0:
            log.error("Ingestion failed:\n%s", result.stderr)
        else:
            log.info("Ingestion completed:\n%s", result.stdout)
        return result.returncode
    except subprocess.TimeoutExpired:
        log.error("Ingestion timed out after 5 minutes")
        return 1
    except Exception as e:
        log.error("Failed to run ingestion: %s", e)
        return 1


def run_extract_entities(dry_run: bool = False) -> int:
    """Execute the entity extraction script."""
    cmd = [sys.executable, "-m", "scripts.extract_entities"]
    if dry_run:
        cmd.append("--dry-run")

    log.info("Running: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=600,  # 10 minute timeout
        )
        if result.returncode != 0:
            log.error("Entity extraction failed:\n%s", result.stderr)
        else:
            log.info("Entity extraction completed:\n%s", result.stdout)
        return result.returncode
    except subprocess.TimeoutExpired:
        log.error("Entity extraction timed out after 10 minutes")
        return 1
    except Exception as e:
        log.error("Failed to run entity extraction: %s", e)
        return 1


def run_wiki_sync(dry_run: bool = False) -> int:
    """Execute the wiki sync script."""
    cmd = [sys.executable, "-m", "scripts.sync_to_wiki"]
    if dry_run:
        cmd.append("--dry-run")

    log.info("Running: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=120,  # 2 minute timeout
        )
        if result.returncode != 0:
            log.error("Wiki sync failed:\n%s", result.stderr)
        else:
            log.info("Wiki sync completed:\n%s", result.stdout)
        return result.returncode
    except subprocess.TimeoutExpired:
        log.error("Wiki sync timed out after 2 minutes")
        return 1
    except Exception as e:
        log.error("Failed to run wiki sync: %s", e)
        return 1


def main() -> int:
    """Main entry point for post-sync hook."""
    parser = argparse.ArgumentParser(description="Post-sync hook for LLM Wiki ingestion")
    parser.add_argument("--force", action="store_true", help="Force trigger even if no new mails")
    parser.add_argument("--dry-run", action="store_true", help="Dry run (no actual changes)")
    parser.add_argument("--skip-extract", action="store_true", help="Skip entity extraction")
    parser.add_argument("--sync-wiki", action="store_true", help="Also sync to LLM Wiki")
    args = parser.parse_args()

    setup_logging()
    log.info("=" * 60)
    log.info("Post-sync hook triggered at %s", datetime.now().isoformat())

    last_run = get_last_run_info()
    if last_run:
        log.info("Last sync run: run_id=%s, status=%s, new_mails=%s",
                 last_run.get("run_id"), last_run.get("status"), last_run.get("new_mails"))

    if not should_trigger_ingest(last_run, force=args.force):
        log.info("No ingestion needed, exiting")
        return 0

    # Run ingestion
    ret = run_ingest(dry_run=args.dry_run)
    if ret != 0:
        log.error("Ingestion failed with code %d", ret)
        return ret

    # Run entity extraction (unless skipped)
    if not args.skip_extract:
        ret = run_extract_entities(dry_run=args.dry_run)
        if ret != 0:
            log.warning("Entity extraction failed with code %d (non-fatal)", ret)
            # Don't fail the whole hook for extraction issues

    # Run wiki sync (if requested)
    if args.sync_wiki:
        ret = run_wiki_sync(dry_run=args.dry_run)
        if ret != 0:
            log.warning("Wiki sync failed with code %d (non-fatal)", ret)

    log.info("Post-sync hook completed successfully")
    return 0


if __name__ == "__main__":
    sys.exit(main())
