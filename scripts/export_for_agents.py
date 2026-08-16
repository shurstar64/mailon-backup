"""Export ingested mail data for external agents.

Provides cursor-based pagination for incremental data export.
Agents can call with a cursor to get only new/updated records.

Usage:
    python -m scripts.export_for_agents [cursor] [limit]
    python -m scripts.export_for_agents 0 500
    python -m scripts.export_for_agents 1000 100

Output format:
    {
        "schema_version": 1,
        "cursor": <last_rowid>,
        "count": <num_items>,
        "items": [...]
    }
"""
import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
INGEST_DB = PROJECT_ROOT / "data" / "ingest.db"


def export(since_rowid: int = 0, limit: int = 500) -> dict:
    """Export ingested files with cursor-based pagination."""
    if not INGEST_DB.exists():
        return {
            "schema_version": 1,
            "cursor": since_rowid,
            "count": 0,
            "items": [],
            "error": "ingest.db not found",
        }

    con = sqlite3.connect(INGEST_DB)
    con.row_factory = sqlite3.Row

    rows = con.execute("""
        SELECT rowid, uid, subject, sender, recv_date, staging_path, content_hash
        FROM ingested_files
        WHERE rowid > ?
        ORDER BY rowid
        LIMIT ?
    """, (since_rowid, limit)).fetchall()

    con.close()

    items = []
    for r in rows:
        items.append({
            "rowid": r["rowid"],
            "uid": r["uid"],
            "subject": r["subject"],
            "sender": r["sender"],
            "date": r["recv_date"],
            "path": r["staging_path"],
            "hash": r["content_hash"],
        })

    return {
        "schema_version": 1,
        "cursor": rows[-1]["rowid"] if rows else since_rowid,
        "count": len(rows),
        "items": items,
    }


def main() -> int:
    cursor = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 500

    result = export(cursor, limit)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
