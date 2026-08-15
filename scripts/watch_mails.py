"""Real-time mail watcher using watchdog.

Monitors mailon-backup data/mails/ directory for new files
and automatically triggers ingestion and wiki sync.

Usage:
    python -m scripts.watch_mails
    python -m scripts.watch_mails --daemon
    python -m scripts.watch_mails --sync-wiki

Requirements:
    pip install watchdog
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from threading import Timer

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
MAILS_DIR = DATA_DIR / "mails"

# Debounce settings
DEBOUNCE_SECONDS = 5.0
_pending_timer: Timer | None = None
_pending_files: set[str] = set()


def run_ingestion() -> None:
    """Run the ingestion script."""
    log.info("Running ingestion for %d new file(s)...", len(_pending_files))
    _pending_files.clear()

    try:
        result = subprocess.run(
            [sys.executable, "-m", "scripts.ingest_mail"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode == 0:
            log.info("Ingestion complete")
        else:
            log.error("Ingestion failed:\n%s", result.stderr)
    except subprocess.TimeoutExpired:
        log.error("Ingestion timed out")
    except Exception as e:
        log.error("Ingestion error: %s", e)


def run_wiki_sync() -> None:
    """Run wiki sync after ingestion."""
    log.info("Running wiki sync...")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "scripts.sync_to_wiki"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0:
            log.info("Wiki sync complete")
        else:
            log.error("Wiki sync failed:\n%s", result.stderr)
    except Exception as e:
        log.error("Wiki sync error: %s", e)


def schedule_ingestion(file_path: str, sync_wiki: bool = False) -> None:
    """Schedule ingestion with debouncing."""
    global _pending_timer

    _pending_files.add(file_path)

    # Cancel existing timer
    if _pending_timer is not None:
        _pending_timer.cancel()

    # Schedule new timer
    def execute():
        run_ingestion()
        if sync_wiki:
            run_wiki_sync()

    _pending_timer = Timer(DEBOUNCE_SECONDS, execute)
    _pending_timer.start()


class MailFileHandler:
    """Handler for file system events."""

    def __init__(self, sync_wiki: bool = False):
        self.sync_wiki = sync_wiki

    def on_created(self, event) -> None:
        if event.is_directory:
            return
        if not event.src_path.endswith('.md'):
            return

        log.debug("New file detected: %s", event.src_path)
        schedule_ingestion(event.src_path, self.sync_wiki)

    def on_modified(self, event) -> None:
        if event.is_directory:
            return
        if not event.src_path.endswith('.md'):
            return

        log.debug("File modified: %s", event.src_path)
        schedule_ingestion(event.src_path, self.sync_wiki)


def run_watcher(sync_wiki: bool = False) -> None:
    """Run the file system watcher."""
    try:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler
    except ImportError:
        log.error("watchdog not installed. Run: pip install watchdog")
        sys.exit(1)

    class WatchdogHandler(FileSystemEventHandler):
        def __init__(self):
            self.handler = MailFileHandler(sync_wiki=sync_wiki)

        def on_created(self, event):
            self.handler.on_created(event)

        def on_modified(self, event):
            self.handler.on_modified(event)

    if not MAILS_DIR.exists():
        log.error("Mails directory does not exist: %s", MAILS_DIR)
        sys.exit(1)

    handler = WatchdogHandler()
    observer = Observer()
    observer.schedule(handler, str(MAILS_DIR), recursive=True)

    log.info("Starting file watcher on: %s", MAILS_DIR)
    log.info("Press Ctrl+C to stop...")

    observer.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Stopping watcher...")
        observer.stop()
    observer.join()


def run_as_daemon() -> None:
    """Run as a background daemon (Windows service-like)."""
    log.info("Running as daemon (use Ctrl+C or kill process to stop)")

    # Create a simple daemon by detaching
    if sys.platform == "win32":
        # On Windows, use subprocess with CREATE_NO_WINDOW
        import subprocess
        DETACHED_PROCESS = 0x00000008
        CREATE_NO_WINDOW = 0x08000000

        subprocess.Popen(
            [sys.executable, "-m", "scripts.watch_mails"],
            creationflags=DETACHED_PROCESS | CREATE_NO_WINDOW,
            cwd=PROJECT_ROOT,
        )
        log.info("Daemon started in background")
    else:
        # On Unix, fork
        pid = os.fork()
        if pid > 0:
            log.info("Daemon started with PID: %d", pid)
            sys.exit(0)

        os.setsid()
        run_watcher()


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Watch mail directory for changes")
    parser.add_argument("--daemon", action="store_true", help="Run as background daemon")
    parser.add_argument("--sync-wiki", action="store_true", help="Also sync to Wiki on changes")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    if args.daemon:
        run_as_daemon()
    else:
        run_watcher(sync_wiki=args.sync_wiki)

    return 0


if __name__ == "__main__":
    sys.exit(main())
