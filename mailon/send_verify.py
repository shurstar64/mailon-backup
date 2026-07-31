"""Fail-closed verification for MailOn compose sends.

The webmail's `beforeSend()` gives no synchronous success signal, so a send
is confirmed by two independent layers:

  Layer 1 (fast-fail, seconds): diff the agent-browser network listing against
  a pre-`beforeSend` watermark of request ids. A NEW send POST with a 4xx/5xx
  status fails immediately; zero new POSTs fails; a missing status is
  tolerated (older agent-browser CLIs never print it) and defers to Layer 2.

  Layer 2 (authoritative): poll the Sent folder (보낸메일함) through the same
  in-page `POST /mail/list_async.json` API the inbox scraper uses, until a
  mail matching (subject, recipients, timeMillis window) appears.

The same Layer-2 probe doubles as a PRE-send idempotency check so that a
retry after a false-negative verification does not send a duplicate.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Callable, Protocol, Sequence
from urllib.parse import urlencode

log = logging.getLogger(__name__)

LIST_ENDPOINT = "/mail/list_async.json"
PAGE_SIZE = 20

# `[id] METHOD URL (resourceType) STATUS?` — STATUS absent until the response
# arrives (and never printed by pre-v0.22 agent-browser CLIs).
_REQUEST_LINE_RE = re.compile(
    r"^\[(?P<id>[^\]]+)\]\s+(?P<method>\S+)\s+(?P<url>\S+)"
    r"\s+\((?P<type>[^)]*)\)(?:\s+(?P<status>\d{3}))?\s*$"
)

_DIAG_JS = r"""
JSON.stringify({
  url: (location.href || '').slice(0, 120),
  rows: document.querySelectorAll('a.mail-metadata').length,
  iframes: Array.from(document.querySelectorAll('iframe'))
    .map(f => (f.src || '').slice(0, 80)).slice(0, 5),
  json_resources: performance.getEntriesByType('resource')
    .map(e => e.name).filter(n => n.includes('json'))
    .map(n => n.slice(0, 120)).slice(0, 25),
  folderish: Array.from(document.querySelectorAll('a,li'))
    .map(e => (e.textContent || '').trim())
    .filter(t => t && t.length < 20 && t.includes('메일함')).slice(0, 15)
})
"""

_ROW_HARVEST_JS = r"""
(() => {
  const a = document.querySelector('a.mail-metadata[onclick*="folderUid"]');
  if (!a) return null;
  const m = (a.getAttribute('onclick')||'').match(/folderUid:'(\d+)'/);
  return m ? m[1] : null;
})()
"""


class SendVerifyError(RuntimeError):
    """Raised when a send cannot be positively confirmed (fail-closed)."""


class VerifyBrowser(Protocol):
    def eval_js(self, script: str) -> str: ...
    def wait_ms(self, milliseconds: int) -> None: ...
    def network_requests(self) -> str: ...
    def network_request_detail(self, request_id: str) -> str: ...


def _unwrap(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith('"') and raw.endswith('"'):
        raw = json.loads(raw)
    return raw


def _page_diag(browser: VerifyBrowser) -> str:
    try:
        return _unwrap(browser.eval_js(_DIAG_JS))[:800]
    except Exception as error:  # diagnostics must never mask the real failure
        return f"diag-failed: {str(error)[:120]}"


def resolve_current_folder_uid(
    browser: VerifyBrowser,
    *,
    timeout_s: float = 90.0,
    poll_interval_s: float = 3.0,
    clock: Callable[[], float] = time.monotonic,
) -> str:
    """Harvest the folderUid of the CURRENT mail list (inbox after login).

    The sidebar never exposes 보낸메일함 to DOM/AX queries (observed
    2026-07-19: 90s of retries, zero hits), so folder verification uses
    allFolder=true instead — all it needs is ANY valid folderUid, harvested
    from a mail row's onclick (the only pattern proven in production sync).
    The SPA renders rows tens of seconds after login: poll until deadline,
    then fail closed WITH page diagnostics for the post-mortem."""
    deadline = clock() + timeout_s
    attempts = 0
    while True:
        attempts += 1
        uid = _unwrap(browser.eval_js(_ROW_HARVEST_JS))
        if uid and uid != "null":
            return uid
        if clock() >= deadline:
            log.warning("mail-list rows never appeared; page diag: %s",
                        _page_diag(browser))
            raise SendVerifyError(
                f"could not resolve current folder uid after {attempts} attempt(s)")
        browser.wait_ms(int(poll_interval_s * 1000))


def _fetch_list_page(
    browser: VerifyBrowser, folder_uid: str, *, all_folders: bool,
) -> list[dict]:
    body = urlencode({
        "tenant": "", "value": "0", "longValue": "0",
        "listCountPerPage": str(PAGE_SIZE), "currentPage": "1",
        "sortField": "timeMillis", "sortDir": "DESC",
        "folderUid": folder_uid,
        "allFolder": "true" if all_folders else "false",
        "includeContent": "false", "page": "1", "start": "0",
        "limit": str(PAGE_SIZE), "sort": "timeMillis", "dir": "DESC",
    })
    js = (
        "(async () => {"
        f"  const r = await fetch({json.dumps(LIST_ENDPOINT)}, {{"
        "     method:'POST',"
        "     credentials:'include',"
        "     headers:{'Content-Type':'application/x-www-form-urlencoded'},"
        f"    body:{json.dumps(body)}"
        "  });"
        "   if (!r.ok) throw new Error('HTTP ' + r.status);"
        "   return await r.text();"
        "})()"
    )
    data = json.loads(_unwrap(browser.eval_js(js)))
    if not data.get("result"):
        log.warning("list_async returned result=false (allFolder=%s)", all_folders)
        return []
    return list(data.get("contents") or [])


def _normalize_millis(value: object) -> int | None:
    try:
        millis = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if millis <= 0:
        return None
    if millis < 10**12:  # server sent seconds, not ms (sanity per design review)
        millis *= 1000
    return millis


def _mail_matches(
    item: dict, subject: str, recipients: Sequence[str], since_millis: int,
) -> bool:
    if str(item.get("subject") or "").strip() != subject.strip():
        return False
    adr_to = str(item.get("adrTo") or "").lower()
    # List rows show ONLY the first recipient for multi-recipient mails, so
    # exact-subject + time-window + ANY-recipient is the strongest check
    # available from list_async (2026-07-20 evidence).
    if not any(r.lower() in adr_to for r in recipients):
        return False
    millis = _normalize_millis(item.get("timeMillis"))
    return millis is not None and millis >= since_millis


def find_mail_match(
    browser: VerifyBrowser,
    folder_uid: str,
    subject: str,
    recipients: Sequence[str],
    since_millis: int,
    *,
    timeout_s: float,
    poll_interval_s: float,
    all_folders: bool = True,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """Poll the mailbox (all folders by default — covers 보낸메일함 without
    resolving its uid) until a matching mail appears. Always probes at least
    once (timeout_s=0 -> single probe, used by the pre-send check)."""
    deadline = clock() + timeout_s
    while True:
        contents = _fetch_list_page(browser, folder_uid, all_folders=all_folders)
        if any(_mail_matches(item, subject, recipients, since_millis)
               for item in contents):
            return True
        if clock() >= deadline:
            rows = [
                {"subject": str(i.get("subject") or "")[:80],
                 "adrTo": str(i.get("adrTo") or "")[:120],
                 "timeMillis": i.get("timeMillis")}
                for i in contents[:5]
            ]
            log.warning("mail match not found (since_millis=%d): top rows=%s; "
                        "page diag: %s", since_millis,
                        json.dumps(rows, ensure_ascii=False), _page_diag(browser))
            return False
        browser.wait_ms(int(poll_interval_s * 1000))


def request_ids(listing: str) -> set[str]:
    """Watermark: ids currently present in the network listing."""
    ids: set[str] = set()
    for line in listing.splitlines():
        parsed = _REQUEST_LINE_RE.match(line.strip())
        if parsed:
            ids.add(parsed.group("id"))
    return ids


def check_network_fast_fail(
    browser: VerifyBrowser,
    baseline_ids: set[str],
    *,
    timeout_s: float,
    poll_interval_s: float = 1.0,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Fast verdict on the send POST fired by beforeSend().

    Only requests NEWER than the watermark count; our own list_async polling
    is excluded. 4xx/5xx -> raise; 2xx/3xx -> return; status never observed
    within the window -> return (tolerated; Layer 2 decides); zero new POSTs
    by the deadline -> raise.
    """
    deadline = clock() + timeout_s
    while True:
        new_posts: list[tuple[str, int | None]] = []
        for line in browser.network_requests().splitlines():
            parsed = _REQUEST_LINE_RE.match(line.strip())
            if not parsed or parsed.group("id") in baseline_ids:
                continue
            if parsed.group("method").upper() != "POST":
                continue
            if LIST_ENDPOINT in parsed.group("url"):
                continue
            status = parsed.group("status")
            new_posts.append((parsed.group("url"), int(status) if status else None))
        for url, status in new_posts:
            if status is not None and status >= 400:
                raise SendVerifyError(f"send POST failed: HTTP {status} {url[:120]}")
        if any(status is not None and status < 400 for _, status in new_posts):
            return
        if clock() >= deadline:
            if not new_posts:
                raise SendVerifyError(
                    "no send POST observed after the send trigger — nothing was sent")
            return  # POST fired, status unknown -> defer to Sent-folder verify
        browser.wait_ms(int(poll_interval_s * 1000))


def log_network_forensics(browser: VerifyBrowser, baseline_ids: set[str]) -> None:
    """Identify every post-trigger request (url/status) and dump the first
    non-list POST's detail (incl. response body head) — best-effort, log-only."""
    try:
        lines: list[str] = []
        detail = ""
        for line in browser.network_requests().splitlines():
            parsed = _REQUEST_LINE_RE.match(line.strip())
            if not parsed or parsed.group("id") in baseline_ids:
                continue
            lines.append(f'{parsed.group("method")} {parsed.group("url")[:100]} '
                         f'{parsed.group("status") or "?"}')
            if (not detail and parsed.group("method").upper() == "POST"
                    and LIST_ENDPOINT not in parsed.group("url")):
                try:
                    detail = browser.network_request_detail(parsed.group("id"))[:1200]
                except Exception as error:
                    detail = f"detail-failed: {str(error)[:100]}"
        log.info("post-trigger network: %s | first-post detail: %s",
                 "; ".join(lines[:10]) or "(none)", detail or "(none)")
    except Exception as error:
        log.warning("network forensics failed: %s", str(error)[:150])
