"""Actual send trigger for the MailOn compose layer.

Root cause locked 2026-07-19: `window._compose.beforeSend()` is a PRE-send
hook — calling it alone has NEVER delivered a mail (the W0-7c self-test of
2026-07-15 never arrived; 0 hits in 757 synced mails). The faithful trigger
is what a human does: click the compose form's 보내기 button, which runs the
full handler chain (validation → beforeSend → actual submit).

This module also dispatches native input/change/blur events on the address
and subject fields first (jQuery-era UIs keep internal models that raw
`.value` writes do not update), and dumps a discovery snapshot of
`window._compose` so every attempt — pass or fail — documents ground truth.
"""
from __future__ import annotations

import json
import logging
from typing import Protocol

log = logging.getLogger(__name__)


class TriggerBrowser(Protocol):
    def eval_js(self, script: str) -> str: ...


def _unwrap(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith('"') and raw.endswith('"'):
        raw = json.loads(raw)
    return raw


_FIELD_EVENTS_JS = r"""
(() => {
  const fire = (el) => ['input','change','blur'].forEach(
    t => el.dispatchEvent(new Event(t, {bubbles: true})));
  const done = [];
  for (const sel of ['#adr-to-ipt_ta', '#adr-cc-ipt_ta', '#compose_subject']) {
    const el = document.querySelector(sel);
    if (!el) continue;
    const proto = el.tagName === 'TEXTAREA'
      ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const desc = Object.getOwnPropertyDescriptor(proto, 'value');
    if (desc && desc.set) desc.set.call(el, el.value);
    fire(el);
    done.push(sel);
  }
  return 'events:' + done.length;
})()
"""

_DISCOVERY_JS = r"""
JSON.stringify((() => {
  const c = window._compose || {};
  const compose_keys = Object.keys(c).slice(0, 60);
  const PRIORITY = ['getForm', 'send', 'prepareSend', 'beforeSend', 'beforeReview',
                    'command', 'validateForm', 'getSendResult'];
  const interesting = {};
  for (const k of PRIORITY) {
    if (!(k in c)) continue;
    try { interesting[k] = String(c[k]).slice(0, k === 'getForm' ? 3000 : 300); } catch (e) { interesting[k] = 'err'; }
  }
  return {compose_keys: compose_keys, interesting: interesting,
          tbar_keys: Object.keys(window._tbar || {}).slice(0, 30)};
})())
"""

# The real send entry point (discovered 2026-07-19 22:21 KST from the live
# compose object): _compose exposes the full chain validateForm ->
# prepareSend -> send. send() is what the UI button ultimately invokes.
_SEND_CALL_JS = r"""
JSON.stringify((() => {
  const c = window._compose || {};
  if (typeof c.send !== 'function') return {called: null, reason: 'no _compose.send'};
  let src = '';
  try { src = String(c.send).slice(0, 400); } catch (e) {}
  try {
    const r = c.send();
    return {called: '_compose.send()', result: String(r).slice(0, 120), src: src};
  } catch (e) {
    return {called: null, reason: 'send() threw: ' + String(e).slice(0, 200), src: src};
  }
})())
"""

# Click the compose form's real send button. Scope search to the form owning
# the send CSRF token first; document-wide fallback only accepts a UNIQUE
# candidate. 전달(forward)/예약/저장/취소 etc. are excluded by design review.
_CLICK_SEND_JS = r"""
JSON.stringify((() => {
  const RX = /(보내기|발송|전송|^send$)/i;
  const BAD = /(예약|임시저장|저장|취소|닫기|삭제|수신확인|전달|미리보기)/;
  const collect = (scope) => {
    const els = Array.from(scope.querySelectorAll(
      'button, input[type=submit], input[type=button], a, [role=button]'));
    const out = [];
    for (const el of els) {
      const label = ((el.textContent || '') + ' ' + (el.value || '') + ' '
                     + (el.id || '') + ' ' + (el.className || '')).trim();
      if (!RX.test(label) || BAD.test(label)) continue;
      const r = el.getBoundingClientRect();
      if (r.width === 0 || r.height === 0) continue;
      out.push({el: el, area: r.width * r.height, label: label.slice(0, 80)});
    }
    out.sort((a, b) => a.area - b.area);
    return out;
  };
  const csrf = document.querySelector('#sendCSRFToken');
  const form = csrf ? csrf.closest('form') : null;
  let hit = null, scope_used = '';
  if (form) {
    const cands = collect(form);
    if (cands.length) { hit = cands[0]; scope_used = 'form'; }
  }
  if (!hit) {
    const cands = collect(document);
    if (cands.length === 1) { hit = cands[0]; scope_used = 'document'; }
    else if (cands.length > 1) {
      return {clicked: null, ambiguous: cands.map(c => c.label).slice(0, 5)};
    }
  }
  if (!hit) return {clicked: null};
  const info = {clicked: hit.label, scope: scope_used,
                tag: hit.el.tagName, id: hit.el.id || '',
                cls: String(hit.el.className).slice(0, 80),
                onclick: String(hit.el.getAttribute('onclick') || '').slice(0, 200)};
  try { hit.el.focus(); } catch (e) {}
  try {
    hit.el.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
    hit.el.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
  } catch (e) {}
  hit.el.click();
  return info;
})())
"""


def sync_field_events(browser: TriggerBrowser) -> None:
    """Re-commit field values through native setters + input/change/blur."""
    try:
        result = _unwrap(browser.eval_js(_FIELD_EVENTS_JS))
        log.info("compose field events dispatched: %s", result)
    except Exception as error:  # best-effort: the click chain may still work
        log.warning("field event sync failed: %s", str(error)[:150])


def discover_compose(browser: TriggerBrowser) -> str:
    """Ground-truth snapshot of the compose JS surface (logged every attempt)."""
    try:
        return _unwrap(browser.eval_js(_DISCOVERY_JS))[:6000]
    except Exception as error:
        return f"discovery-failed: {str(error)[:120]}"


def click_send_button(browser: TriggerBrowser) -> str | None:
    """Click the real 보내기 button. Returns a description of what was
    clicked, or None (button missing/ambiguous — caller must fail closed)."""
    raw = _unwrap(browser.eval_js(_CLICK_SEND_JS))
    try:
        info = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        log.warning("send-button click returned unparseable: %s", str(raw)[:200])
        return None
    if not isinstance(info, dict) or not info.get("clicked"):
        log.warning("send button not found/ambiguous: %s", json.dumps(
            info, ensure_ascii=False)[:400] if isinstance(info, dict) else raw[:200])
        return None
    description = json.dumps(info, ensure_ascii=False)[:400]
    log.info("send button clicked: %s", description)
    return description


def call_compose_send(browser: TriggerBrowser) -> str | None:
    """Invoke `window._compose.send()` — the real send entry point. Returns
    a description on success, None when unavailable/threw (caller falls back
    to the button click, then fails closed)."""
    raw = _unwrap(browser.eval_js(_SEND_CALL_JS))
    try:
        info = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        log.warning("_compose.send() call returned unparseable: %s", str(raw)[:200])
        return None
    if not isinstance(info, dict) or not info.get("called"):
        log.warning("_compose.send() unavailable: %s",
                    json.dumps(info, ensure_ascii=False)[:500]
                    if isinstance(info, dict) else str(raw)[:200])
        return None
    description = json.dumps(info, ensure_ascii=False)[:500]
    log.info("send triggered via _compose.send(): %s", description)
    return description


_FORM_PROBE_JS = r"""
JSON.stringify((() => { /* compose form probe */
  const c = window._compose || {};
  if (typeof c.getForm !== 'function') return {__probe_error: 'getForm unavailable'};
  let p = null;
  try { p = c.getForm(); } catch (e) { return {__probe_error: String(e).slice(0, 150)}; }
  if (!p || typeof p !== 'object') return {__probe_error: 'getForm() returned ' + String(p)};
  const out = {};
  for (const k of Object.keys(p).slice(0, 60)) {
    try { out[k] = String(p[k]).slice(0, 200); } catch (e) { out[k] = 'err'; }
  }
  return out;
})())
"""

_EDITOR_API_FILL_TEMPLATE = r"""
(() => {
  const html = __HTML__;
  const wrapped = '<div style="font-family:\uad74\ub9bc; font-size:10pt; line-height:150%">'
                  + html + '</div>';
  const done = [];
  const report = [];
  const ed = window.mail_editor;  // getForm(): param.content = mail_editor.getContent()
  if (!ed) return 'refill:[] | no window.mail_editor';
  if (typeof ed.setContent === 'function') {
    try { ed.setContent(html); done.push('setContent'); }
    catch (e) { report.push('setContent:' + String(e).slice(0, 60)); }
  }
  let after = '';
  try { after = String(ed.getContent()); } catch (e) { after = 'err:' + String(e).slice(0, 60); }
  if (after.indexOf('undefined') !== -1 || !after) {
    // editor engine dead in automation: override the exact accessor getForm uses
    ed.getContent = function() { return wrapped; };
    if (typeof ed.getMode === 'function') {
      const origMode = (() => { try { return ed.getMode(); } catch (e) { return 'html'; } })();
      ed.getMode = function() { return origMode === 'text' ? 'text' : 'html'; };
    }
    done.push('getContent-override');
    try { after = String(ed.getContent()); } catch (e) {}
  }
  return 'refill:[' + done.join(',') + '] | getContent-after: ' + after.slice(0, 150)
         + ' | ' + report.join(' / ').slice(0, 200);
})()
"""


def _body_marker(body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:30]
    return body.strip()[:30]


def verify_compose_form(
    browser: TriggerBrowser, recipients, body: str,
) -> tuple[bool, str]:
    """Probe `_compose.getForm()` and require the REAL outgoing params to
    contain our body and route to our recipients (2026-07-19: a raw-DOM body
    write left getForm() with 'undefined', which was then literally sent)."""
    raw = _unwrap(browser.eval_js(_FORM_PROBE_JS))
    try:
        form = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return False, f"unparseable form probe: {str(raw)[:200]}"
    if not isinstance(form, dict):
        return False, f"non-dict form probe: {str(raw)[:200]}"
    dump = json.dumps(form, ensure_ascii=False)
    if form.get("__probe_error"):
        return False, dump
    marker = _body_marker(body)
    body_ok = bool(marker) and any(
        marker in str(v) for k, v in form.items() if k != "__probe_error")
    if str(form.get("method", "")) == "tome":
        # send() rewrites to := from in tome mode; only safe for true self-send
        sender = str(form.get("from", "")).lower()
        to_ok = all(r.lower() in sender for r in recipients)
    else:
        to_ok = all(r.lower() in dump.lower() for r in recipients)
    return body_ok and to_ok, dump


def fill_body_via_editor_api(browser: TriggerBrowser, body: str) -> None:
    """Re-commit the body through the Namo CrossEditor API / hidden field."""
    import html as html_module
    html_body = html_module.escape(body).replace("\n", "<br>")
    script = _EDITOR_API_FILL_TEMPLATE.replace("__HTML__", json.dumps(html_body))
    try:
        result = _unwrap(browser.eval_js(script))
        log.info("editor-API body refill: %s", str(result)[:200])
    except Exception as error:
        log.warning("editor-API body refill failed: %s", str(error)[:150])


_RECIPIENT_TOKENIZE_TEMPLATE = r"""
(() => {
  const groups = __GROUPS__;
  const report = [];
  for (const pair of groups) {
    const sel = pair[0], addrs = pair[1];
    const el = document.querySelector(sel);
    if (!el) { report.push(sel + ':missing'); continue; }
    const proto = el.tagName === 'TEXTAREA'
      ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const desc = Object.getOwnPropertyDescriptor(proto, 'value');
    for (const addr of addrs) {
      try { el.focus(); } catch (e) {}
      if (desc && desc.set) desc.set.call(el, addr); else el.value = addr;
      el.dispatchEvent(new Event('input', {bubbles: true}));
      for (const type of ['keydown', 'keyup']) {
        el.dispatchEvent(new KeyboardEvent(type,
          {key: 'Enter', keyCode: 13, which: 13, bubbles: true}));
      }
      el.dispatchEvent(new Event('change', {bubbles: true}));
      try { el.blur(); } catch (e) {}
      el.dispatchEvent(new Event('blur', {bubbles: true}));
    }
    report.push(sel + ':' + addrs.length + ' fed');
  }
  const named = document.querySelectorAll(
    '#mcp_wrap [name="to"], #mcp_wrap [name="cc"]');
  report.push('named-recipient-fields:' + named.length);
  return report.join(' | ');
})()
"""


def commit_recipients(browser: TriggerBrowser, recipients, cc) -> None:
    """Feed addresses ONE AT A TIME with Enter tokenization (2026-07-20:
    a comma-joined multi-recipient fill never registered any chip, so
    getForm() carried zero recipients and the form gate refused to send)."""
    groups = [["#adr-to-ipt_ta", list(recipients)]]
    if cc:
        groups.append(["#adr-cc-ipt_ta", list(cc)])
    script = _RECIPIENT_TOKENIZE_TEMPLATE.replace("__GROUPS__", json.dumps(groups))
    try:
        log.info("recipient tokenization: %s", _unwrap(browser.eval_js(script))[:300])
    except Exception as error:  # gate still verifies via getForm afterwards
        log.warning("recipient tokenization failed: %s", str(error)[:150])
