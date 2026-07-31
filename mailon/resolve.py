"""Recipient name -> email resolution via the compose To-field autocomplete.

Read-only flow: open the compose screen, type the query into the To field,
scrape the Crinity autocomplete grid (#ac_toGrid .cr_cellTmplCommon) and
parse its cells into structured candidates. No send trigger is ever touched.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from .browser import BrowserError

log = logging.getLogger(__name__)

# Grid cell shapes observed in the wild (fixtures use dummy data):
#   Organization\n"김샘플" <k@example.invalid>\n:: 연구지원팀
#   Contacts "김샘플" <k@example.invalid>
#   대표 도메인 "김샘플" <k@example.invalid> :: 예시연구원 - 연구지원팀
_NAME_EMAIL_RE = re.compile(r'"([^"]+)"\s*<([^>]+)>')

# Prefix label (before the quoted name) -> candidate group.
_GROUP_PREFIXES = (
    ("Organization", "organization"),
    ("Contacts", "contacts"),
    ("대표 도메인", "history"),
)

_TO_FIELD = "#adr-to-ipt_ta"

_CELLS_JS = (
    "JSON.stringify(Array.from(document.querySelectorAll("
    "'#ac_toGrid .cr_cellTmplCommon')).map("
    "function(c){return c.innerText||'';}))"
)

# Fallback typing: commit the value through the native prototype setter and
# fire key events so the autocomplete widget notices the change.
_FALLBACK_TYPE_JS = """
(function() {
  var field = document.querySelector('#adr-to-ipt_ta');
  if (!field) { return 'no-to-field'; }
  var setter = Object.getOwnPropertyDescriptor(
      window.HTMLTextAreaElement.prototype, 'value').set;
  setter.call(field, %s);
  field.focus();
  ['keydown', 'input', 'keyup'].forEach(function(type) {
    field.dispatchEvent(new KeyboardEvent(type, {bubbles: true}));
  });
  return 'fallback-typed';
})();
"""


class ResolveBrowser(Protocol):
    def eval_js(self, script: str) -> str: ...
    def eval_json(self, script: str): ...
    def focus(self, ref: str) -> None: ...
    def type_text(self, ref: str, value: str) -> None: ...
    def wait_ms(self, milliseconds: int) -> None: ...
    def clear_network_requests(self) -> None: ...
    def network_post_count(self) -> int: ...


@dataclass(frozen=True)
class Candidate:
    group: str
    name: str
    email: str
    org: str


def parse_candidates(cell_texts: Sequence[str], query: str) -> tuple[Candidate, ...]:
    """Parse autocomplete grid cell texts into deduped, ordered candidates.

    `query` is part of the call contract for diagnostics only; the grid is
    already filtered by the webmail, so we never filter on it here.
    """
    out: list[Candidate] = []
    seen: set[tuple[str, str, str]] = set()
    for text in cell_texts:
        match = _NAME_EMAIL_RE.search(text)
        if match is None:
            continue  # placeholder ("There is no content...") / patternless cell
        prefix = text[:match.start()]
        group = "unknown"
        for label, mapped in _GROUP_PREFIXES:
            if label in prefix:
                group = mapped
                break
        suffix = text[match.end():]
        org = suffix.rsplit("::", 1)[1].strip() if "::" in suffix else ""
        key = (match.group(1), match.group(2), group)
        if key in seen:
            continue
        seen.add(key)
        out.append(Candidate(group=group, name=match.group(1),
                             email=match.group(2), org=org))
    log.debug("parse_candidates(%r): %d cell(s) -> %d candidate(s)",
              query, len(cell_texts), len(out))
    return tuple(out)


def resolve_name(browser: ResolveBrowser, query: str) -> tuple[list[str], int]:
    """Open compose, type `query` into To, scrape the autocomplete grid.

    Returns (cell_texts, post_count). Read-only: only the compose screen is
    opened; nothing is submitted.
    """
    log.debug("resolve_name: opening compose for query %r", query)
    browser.eval_js("window._tbar.compose(); 'compose-opened';")
    browser.wait_ms(3000)
    browser.clear_network_requests()
    try:
        browser.focus(_TO_FIELD)
        browser.type_text(_TO_FIELD, query)
    except BrowserError:
        log.debug("resolve_name: focus/type_text failed; eval_js fallback")
        browser.eval_js(_FALLBACK_TYPE_JS % json.dumps(query))
    browser.wait_ms(2500)
    raw = browser.eval_json(_CELLS_JS)
    cells = [str(cell) for cell in raw] if isinstance(raw, list) else []
    post_count = browser.network_post_count()
    log.debug("resolve_name: %d cell(s) scraped, %d POST(s) observed",
              len(cells), post_count)
    return cells, post_count
