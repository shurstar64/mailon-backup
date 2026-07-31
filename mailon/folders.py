"""Folder-uid resolution for non-inbox folders (보낸메일함 sync).

The MailOn SPA keeps the folder sidebar OUT of the main frame (2026-07-19
evidence: '보낸메일함' never appears in main-doc DOM/AX), so resolution uses
two strategies:

  a. cross-frame DOM harvest — scan the main document AND every same-origin
     iframe contentDocument for the folder name, then take the folderUid
     digits nearest to that text.

  b. allFolder inference (Sent only) — POST /mail/list_async.json with
     allFolder=true returns rows across ALL folders (proven by the send
     verification pipeline); rows authored by the account owner and addressed
     to someone else live in 보낸메일함, so the dominant non-inbox folderUid
     among them is the Sent folder. 내게쓴메일함 rows (self→self) are excluded.

Both strategies failing returns None — callers fail open (sync continues
with the folders it can reach).
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from typing import Protocol
from urllib.parse import urlencode

log = logging.getLogger(__name__)

LIST_ENDPOINT = "/mail/list_async.json"
PAGE_SIZE = 20
_MAX_INFERENCE_PAGES = 3
_MIN_HITS = 2


class FolderBrowser(Protocol):
    def eval_js(self, script: str) -> str: ...


def _unwrap(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith('"') and raw.endswith('"'):
        raw = json.loads(raw)
    return raw


_FRAME_HARVEST_TEMPLATE = r"""
(() => {
  const NAME = __NAME__;
  const RX = /folderUid[^0-9]{0,8}(\d{3,})/g;
  const docs = [document];
  for (const f of document.querySelectorAll('iframe')) {
    try { if (f.contentDocument) docs.push(f.contentDocument); } catch (e) {}
  }
  for (const doc of docs) {
    let nodes = [];
    try { nodes = Array.from(doc.querySelectorAll('a,li,span,div')); } catch (e) { continue; }
    for (const el of nodes) {
      const t = (el.textContent || '').trim();
      if (!(t === NAME || (t.startsWith(NAME) && t.length <= 30))) continue;
      let cur = el;
      for (let depth = 0; cur && depth < 5; depth++) {
        const html = cur.outerHTML || '';
        const anchor = html.indexOf(NAME);
        let best = null, bestDist = Infinity, m;
        RX.lastIndex = 0;
        while ((m = RX.exec(html)) !== null) {
          const d = anchor >= 0 ? Math.abs(m.index - anchor) : m.index;
          if (d < bestDist) { bestDist = d; best = m[1]; }
        }
        if (best) return best;
        cur = cur.parentElement;
      }
    }
  }
  return null;
})()
"""


def _harvest_from_frames(browser: FolderBrowser, folder_name: str) -> str | None:
    script = _FRAME_HARVEST_TEMPLATE.replace("__NAME__", json.dumps(folder_name))
    try:
        uid = _unwrap(browser.eval_js(script))
    except Exception as error:  # harvest is best-effort; inference may still work
        log.warning("folder harvest failed for %s: %s", folder_name, str(error)[:150])
        return None
    if uid and uid != "null":
        return uid
    return None


def _fetch_all_folder_page(
    browser: FolderBrowser, inbox_uid: str, page: int,
) -> list[dict]:
    body = urlencode({
        "tenant": "", "value": "0", "longValue": "0",
        "listCountPerPage": str(PAGE_SIZE), "currentPage": str(page),
        "sortField": "timeMillis", "sortDir": "DESC",
        "folderUid": inbox_uid, "allFolder": "true",
        "includeContent": "false", "page": str(page),
        "start": str((page - 1) * PAGE_SIZE), "limit": str(PAGE_SIZE),
        "sort": "timeMillis", "dir": "DESC",
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
        return []
    return list(data.get("contents") or [])


def _infer_sent_uid(
    browser: FolderBrowser, *, inbox_uid: str, account_email: str,
) -> str | None:
    me = account_email.lower()
    counts: Counter[str] = Counter()
    sampled = 0
    for page in range(1, _MAX_INFERENCE_PAGES + 1):
        try:
            contents = _fetch_all_folder_page(browser, inbox_uid, page)
        except Exception as error:
            log.warning("allFolder inference fetch failed (page %d): %s",
                        page, str(error)[:150])
            break
        if not contents:
            break
        sampled += len(contents)
        for item in contents:
            folder_uid = str(item.get("folderUid") or "")
            adr_from = str(item.get("adrFrom") or "").lower()
            adr_to = str(item.get("adrTo") or "").lower()
            if not folder_uid or folder_uid == str(inbox_uid):
                continue
            if me not in adr_from or me in adr_to:
                continue  # not an outgoing-to-others row (excludes 내게쓴메일함)
            counts[folder_uid] += 1
        if len(contents) < PAGE_SIZE:
            break
    if not counts:
        log.warning("sent-folder inference found no candidate rows "
                    "(sampled=%d rows)", sampled)
        return None
    ranked = counts.most_common()
    top_uid, top_hits = ranked[0]
    if top_hits < _MIN_HITS or (len(ranked) > 1 and ranked[1][1] == top_hits):
        log.warning("sent-folder inference inconclusive: %s (sampled=%d)",
                    dict(counts), sampled)
        return None
    return top_uid


def resolve_folder_uid(
    browser: FolderBrowser,
    folder_name: str,
    *,
    inbox_uid: str,
    account_email: str,
) -> str | None:
    """Resolve a folder's uid; None = unresolvable (caller must fail open)."""
    uid = _harvest_from_frames(browser, folder_name)
    if uid:
        log.info("folder %s resolved via frame harvest: %s", folder_name, uid)
        return uid
    if folder_name.startswith("보낸"):
        uid = _infer_sent_uid(browser, inbox_uid=inbox_uid,
                              account_email=account_email)
        if uid:
            log.info("folder %s resolved via allFolder inference: %s",
                     folder_name, uid)
            return uid
    log.warning("folder %s unresolvable: harvest AND inference both failed — "
                "skipping this folder (fail-open)", folder_name)
    return None
