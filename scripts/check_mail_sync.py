"""Status checker for mail sync and ingestion.

Validates:
- mailon-backup sync state (runs, messages, attachments)
- LLM Wiki ingestion state (ingested files, entities)
- Staging directory integrity
- Identifies potential issues

Usage:
    python -m scripts.check_mail_sync
    python -m scripts.check_mail_sync --verbose
    python -m scripts.check_mail_sync --json
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
STATE_DB = DATA_DIR / "state.db"
INGEST_DB = DATA_DIR / "ingest.db"
ENTITY_DB = DATA_DIR / "entities.db"
MAILS_DIR = DATA_DIR / "mails"
ATTACHMENTS_DIR = DATA_DIR / "attachments"
STAGING_DIR = PROJECT_ROOT / "staging"
LOGS_DIR = PROJECT_ROOT / "logs"


@dataclass
class CheckResult:
    """Result of a single check."""
    name: str
    status: str  # ok, warning, error
    message: str
    details: dict = field(default_factory=dict)


@dataclass
class SyncStatus:
    """Overall sync status."""
    timestamp: datetime
    checks: list[CheckResult] = field(default_factory=list)
    warnings: int = 0
    errors: int = 0

    def add(self, check: CheckResult) -> None:
        self.checks.append(check)
        if check.status == "warning":
            self.warnings += 1
        elif check.status == "error":
            self.errors += 1

    @property
    def overall_status(self) -> str:
        if self.errors > 0:
            return "error"
        if self.warnings > 0:
            return "warning"
        return "ok"


# ---- Database Helpers ----

@contextmanager
def db_conn(path: Path):
    """Context manager for SQLite connection."""
    if not path.exists():
        yield None
        return
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def get_table_count(conn, table: str) -> int:
    """Get row count for a table."""
    try:
        row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
        return row["n"] if row else 0
    except sqlite3.OperationalError:
        return -1


# ---- Check Functions ----

def check_state_db() -> CheckResult:
    """Check mailon state.db health."""
    if not STATE_DB.exists():
        return CheckResult(
            name="state_db",
            status="error",
            message="state.db does not exist",
            details={"path": str(STATE_DB)}
        )

    with db_conn(STATE_DB) as conn:
        if conn is None:
            return CheckResult(
                name="state_db",
                status="error",
                message="Cannot connect to state.db"
            )

        messages = get_table_count(conn, "messages")
        attachments = get_table_count(conn, "attachments")
        runs = get_table_count(conn, "runs")

        # Check last run
        last_run = conn.execute(
            "SELECT * FROM runs ORDER BY run_id DESC LIMIT 1"
        ).fetchone()

        details = {
            "messages": messages,
            "attachments": attachments,
            "runs": runs,
            "size_mb": round(STATE_DB.stat().st_size / 1024 / 1024, 2),
        }

        if last_run:
            details["last_run"] = {
                "run_id": last_run["run_id"],
                "status": last_run["status"],
                "new_mails": last_run["new_mails"],
                "finished_at": last_run["finished_at"],
            }

            if last_run["status"] == "fail":
                return CheckResult(
                    name="state_db",
                    status="warning",
                    message=f"Last sync run failed: {last_run['error']}",
                    details=details
                )

        # Check attachment failure rate
        att_stats = conn.execute(
            "SELECT status, COUNT(*) AS n FROM attachments GROUP BY status"
        ).fetchall()

        stats = {r["status"]: r["n"] for r in att_stats}
        details["attachment_stats"] = stats

        total_att = sum(stats.values())
        fail_count = stats.get("fail", 0)

        if total_att > 0 and fail_count / total_att > 0.2:
            return CheckResult(
                name="state_db",
                status="warning",
                message=f"High attachment failure rate: {fail_count}/{total_att}",
                details=details
            )

        return CheckResult(
            name="state_db",
            status="ok",
            message=f"OK: {messages} messages, {attachments} attachments",
            details=details
        )


def check_ingest_db() -> CheckResult:
    """Check ingest.db health."""
    if not INGEST_DB.exists():
        return CheckResult(
            name="ingest_db",
            status="warning",
            message="ingest.db does not exist (run ingestion first)",
            details={"path": str(INGEST_DB)}
        )

    with db_conn(INGEST_DB) as conn:
        if conn is None:
            return CheckResult(
                name="ingest_db",
                status="error",
                message="Cannot connect to ingest.db"
            )

        ingested = get_table_count(conn, "ingested_files")

        details = {
            "ingested_files": ingested,
            "size_kb": round(INGEST_DB.stat().st_size / 1024, 2),
        }

        return CheckResult(
            name="ingest_db",
            status="ok",
            message=f"OK: {ingested} files ingested",
            details=details
        )


def check_entity_db() -> CheckResult:
    """Check entity.db health."""
    if not ENTITY_DB.exists():
        return CheckResult(
            name="entity_db",
            status="warning",
            message="entities.db does not exist (run extraction first)",
            details={"path": str(ENTITY_DB)}
        )

    with db_conn(ENTITY_DB) as conn:
        if conn is None:
            return CheckResult(
                name="entity_db",
                status="error",
                message="Cannot connect to entities.db"
            )

        entities = get_table_count(conn, "entities")
        mentions = get_table_count(conn, "entity_mentions")

        # Get entity type breakdown
        type_stats = conn.execute(
            "SELECT entity_type, COUNT(*) AS n FROM entities GROUP BY entity_type"
        ).fetchall()

        details = {
            "total_entities": entities,
            "total_mentions": mentions,
            "by_type": {r["entity_type"]: r["n"] for r in type_stats},
            "size_kb": round(ENTITY_DB.stat().st_size / 1024, 2),
        }

        return CheckResult(
            name="entity_db",
            status="ok",
            message=f"OK: {entities} entities, {mentions} mentions",
            details=details
        )


def check_mails_dir() -> CheckResult:
    """Check data/mails directory."""
    if not MAILS_DIR.exists():
        return CheckResult(
            name="mails_dir",
            status="error",
            message="data/mails directory does not exist",
            details={"path": str(MAILS_DIR)}
        )

    md_files = list(MAILS_DIR.rglob("*.md"))
    total_size = sum(f.stat().st_size for f in md_files)

    details = {
        "path": str(MAILS_DIR),
        "file_count": len(md_files),
        "total_size_mb": round(total_size / 1024 / 1024, 2),
    }

    if len(md_files) == 0:
        return CheckResult(
            name="mails_dir",
            status="warning",
            message="No mail files found",
            details=details
        )

    return CheckResult(
        name="mails_dir",
        status="ok",
        message=f"OK: {len(md_files)} mail files ({details['total_size_mb']} MB)",
        details=details
    )


def check_attachments_dir() -> CheckResult:
    """Check data/attachments directory."""
    if not ATTACHMENTS_DIR.exists():
        return CheckResult(
            name="attachments_dir",
            status="warning",
            message="data/attachments directory does not exist",
            details={"path": str(ATTACHMENTS_DIR)}
        )

    # Count folders (each folder = one mail UID)
    folders = [d for d in ATTACHMENTS_DIR.iterdir() if d.is_dir()]
    files = list(ATTACHMENTS_DIR.rglob("*"))
    files = [f for f in files if f.is_file()]
    total_size = sum(f.stat().st_size for f in files)

    details = {
        "path": str(ATTACHMENTS_DIR),
        "folder_count": len(folders),
        "file_count": len(files),
        "total_size_mb": round(total_size / 1024 / 1024, 2),
        "total_size_gb": round(total_size / 1024 / 1024 / 1024, 2),
    }

    return CheckResult(
        name="attachments_dir",
        status="ok",
        message=f"OK: {len(files)} files in {len(folders)} folders ({details['total_size_gb']} GB)",
        details=details
    )


def check_staging_dir() -> CheckResult:
    """Check staging directory."""
    if not STAGING_DIR.exists():
        return CheckResult(
            name="staging_dir",
            status="warning",
            message="staging directory does not exist",
            details={"path": str(STAGING_DIR)}
        )

    # Count files in each subdirectory
    counts = {}
    for subdir in ["mail", "wiki/people", "wiki/projects", "wiki/meetings", "briefs"]:
        path = STAGING_DIR / subdir
        if path.exists():
            files = list(path.rglob("*.md"))
            counts[subdir] = len(files)
        else:
            counts[subdir] = 0

    details = {
        "path": str(STAGING_DIR),
        "counts": counts,
    }

    total = sum(counts.values())
    if total == 0:
        return CheckResult(
            name="staging_dir",
            status="warning",
            message="Staging directory is empty",
            details=details
        )

    return CheckResult(
        name="staging_dir",
        status="ok",
        message=f"OK: {total} files ({', '.join(f'{k}:{v}' for k, v in counts.items())})",
        details=details
    )


def check_sync_recency() -> CheckResult:
    """Check if sync has run recently."""
    if not STATE_DB.exists():
        return CheckResult(
            name="sync_recency",
            status="warning",
            message="Cannot check recency (no state.db)"
        )

    with db_conn(STATE_DB) as conn:
        if conn is None:
            return CheckResult(
                name="sync_recency",
                status="error",
                message="Cannot connect to state.db"
            )

        last_run = conn.execute(
            "SELECT finished_at FROM runs WHERE status='ok' ORDER BY run_id DESC LIMIT 1"
        ).fetchone()

        if not last_run or not last_run["finished_at"]:
            return CheckResult(
                name="sync_recency",
                status="warning",
                message="No successful sync runs found"
            )

        last_sync = datetime.fromtimestamp(last_run["finished_at"])
        age = datetime.now() - last_sync
        age_hours = age.total_seconds() / 3600

        details = {
            "last_sync": last_sync.isoformat(),
            "age_hours": round(age_hours, 1),
        }

        if age_hours > 48:
            return CheckResult(
                name="sync_recency",
                status="warning",
                message=f"Last sync was {age_hours:.1f} hours ago",
                details=details
            )

        return CheckResult(
            name="sync_recency",
            status="ok",
            message=f"OK: Last sync {age_hours:.1f} hours ago",
            details=details
        )


def check_logs() -> CheckResult:
    """Check log files."""
    if not LOGS_DIR.exists():
        return CheckResult(
            name="logs",
            status="warning",
            message="logs directory does not exist"
        )

    log_files = list(LOGS_DIR.glob("*.log"))

    if not log_files:
        return CheckResult(
            name="logs",
            status="warning",
            message="No log files found"
        )

    # Check most recent log
    most_recent = max(log_files, key=lambda f: f.stat().st_mtime)
    age = datetime.now() - datetime.fromtimestamp(most_recent.stat().st_mtime)

    # Check for errors in recent logs
    error_count = 0
    try:
        content = most_recent.read_text(encoding="utf-8", errors="replace")
        error_count = content.lower().count("[error]")
    except Exception:
        pass

    details = {
        "log_count": len(log_files),
        "most_recent": most_recent.name,
        "recent_errors": error_count,
    }

    if error_count > 10:
        return CheckResult(
            name="logs",
            status="warning",
            message=f"{error_count} errors in recent log",
            details=details
        )

    return CheckResult(
        name="logs",
        status="ok",
        message=f"OK: {len(log_files)} log files",
        details=details
    )


# ---- Main ----

def run_all_checks() -> SyncStatus:
    """Run all status checks."""
    status = SyncStatus(timestamp=datetime.now())

    checks = [
        check_state_db,
        check_ingest_db,
        check_entity_db,
        check_mails_dir,
        check_attachments_dir,
        check_staging_dir,
        check_sync_recency,
        check_logs,
    ]

    for check_fn in checks:
        try:
            result = check_fn()
            status.add(result)
        except Exception as e:
            status.add(CheckResult(
                name=check_fn.__name__,
                status="error",
                message=f"Check failed: {e}"
            ))

    return status


def format_status_text(status: SyncStatus) -> str:
    """Format status as text."""
    lines = [
        f"Mail Sync Status Check",
        f"Time: {status.timestamp.isoformat()}",
        f"Overall: {status.overall_status.upper()}",
        f"Warnings: {status.warnings}, Errors: {status.errors}",
        "=" * 60,
        "",
    ]

    for check in status.checks:
        icon = {"ok": "[OK]", "warning": "[WARN]", "error": "[ERR]"}[check.status]
        lines.append(f"{icon} {check.name}: {check.message}")

    return "\n".join(lines)


def format_status_json(status: SyncStatus) -> str:
    """Format status as JSON."""
    return json.dumps({
        "timestamp": status.timestamp.isoformat(),
        "overall_status": status.overall_status,
        "warnings": status.warnings,
        "errors": status.errors,
        "checks": [
            {
                "name": c.name,
                "status": c.status,
                "message": c.message,
                "details": c.details,
            }
            for c in status.checks
        ],
    }, ensure_ascii=False, indent=2)


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Check mail sync status")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    status = run_all_checks()

    if args.json:
        print(format_status_json(status))
    else:
        print(format_status_text(status))

    # Exit code based on status
    if status.errors > 0:
        return 2
    if status.warnings > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
