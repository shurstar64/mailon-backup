"""mailon.kr login flow.

Based on reverse-engineering the login form at https://mailon.kr/integrated/login:

  textbox[name=ipt-id]   -> email id
  textbox[name=ipt-pw]   -> password
  textbox[name=ipt-otp]  -> 6-digit TOTP
  button "로그인"         -> submit

The form does RSA encryption of id/pw client-side and POSTs to
/integrated/login. On success, the server responds with JSON containing
`redirectURL`; the page then navigates to the mailbox.

We do NOT try to bypass the form (would require reproducing RSA in Python).
We let the browser's own JS run; we just fill fields and click.
"""
from __future__ import annotations

import logging
import time

from .browser import AgentBrowser, BrowserError
from .config import Config
from .totp import generate_code, seconds_until_next_code


log = logging.getLogger(__name__)


class LoginError(RuntimeError):
    pass


def _find_login_refs(browser: AgentBrowser) -> dict[str, str]:
    """Inspect the login form, return refs for {id, pw, otp, submit}.

    The snapshot shows three textboxes with ref e17/e18/e19 and an OTP
    placeholder. We map them by order + the known OTP placeholder.
    """
    snap = browser.snapshot_json(interactive_only=True)
    # snap is a tree; walk it
    refs: list[tuple[str, str, dict]] = []  # (role, ref, node)
    buttons: list[tuple[str, dict]] = []

    def walk(node):
        if isinstance(node, dict):
            role = node.get("role") or node.get("type") or ""
            ref = node.get("ref") or node.get("id") or ""
            if role in ("textbox", "input"):
                refs.append((role, ref, node))
            if role == "button":
                buttons.append((ref, node))
            for child in node.get("children") or []:
                walk(child)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(snap)

    if len(refs) < 3:
        raise LoginError(
            f"expected 3 input fields on login form, found {len(refs)}. "
            "Page structure may have changed."
        )

    # Heuristic mapping: the form has id, pw, otp in that order.
    # OTP field has placeholder/name containing 'otp'.
    id_ref = pw_ref = otp_ref = None

    # Prefer explicit name/placeholder/aria hints
    for role, ref, node in refs:
        text = json_text_blob(node).lower()
        if "otp" in text:
            otp_ref = ref
            break

    remaining = [(ref, node) for role, ref, node in refs if ref != otp_ref]
    if len(remaining) >= 2:
        id_ref = remaining[0][0]
        pw_ref = remaining[1][0]

    if not (id_ref and pw_ref and otp_ref):
        raise LoginError(
            f"could not identify id/pw/otp fields: id={id_ref} pw={pw_ref} otp={otp_ref}"
        )

    # Find login button by name "로그인"
    submit_ref = None
    for ref, node in buttons:
        if "로그인" in json_text_blob(node):
            submit_ref = ref
            break
    if not submit_ref:
        # fallback: first button
        submit_ref = buttons[0][0] if buttons else None
    if not submit_ref:
        raise LoginError("login button not found")

    return {"id": id_ref, "pw": pw_ref, "otp": otp_ref, "submit": submit_ref}


def json_text_blob(node) -> str:
    """Concatenate all stringy fields of a node for keyword search."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return " ".join(json_text_blob(x) for x in node)
    if isinstance(node, dict):
        parts = []
        for k, v in node.items():
            if k == "children":
                continue
            parts.append(json_text_blob(v))
        return " ".join(parts)
    return str(node)


def login(browser: AgentBrowser, cfg: Config) -> None:
    """Perform a fresh login. Raises LoginError on failure.

    Strategy:
      1. Navigate to login URL
      2. Close any "notice" popups
      3. Wait for the TOTP window to have >=5 seconds left so code doesn't
         expire mid-flight
      4. Fill id, pw, otp
      5. Submit
      6. Wait for URL change away from /integrated/login
    """
    # Check if already logged in (on mail page)
    try:
        current_url = browser.current_url()
        if "/mail" in current_url and "mailon.kr" in current_url:
            log.info("already logged in at %s; skipping login flow", current_url)
            return
    except BrowserError:
        pass  # No page loaded yet, proceed with login

    log.info("opening login page: %s", cfg.login_url)
    browser.open(cfg.login_url)
    log.info("opened; waiting for DOM")
    try:
        browser.wait_load("domcontentloaded")
    except BrowserError as e:
        log.warning("wait_load(domcontentloaded) failed: %s", e)

    log.info("dismissing popups")
    _dismiss_popups(browser)
    log.info("popups dismissed; locating fields")

    # Wait until current TOTP window has at least 10 seconds left.
    # The network round-trip + RSA encryption can eat several seconds;
    # if the code expires before the server validates it, the response
    # is indistinguishable from a wrong-password error. Give ourselves
    # a generous margin.
    remaining = seconds_until_next_code()
    if remaining < 10:
        log.info("TOTP window has %ds left (<10); waiting for next window", remaining)
        time.sleep(remaining + 1)

    code = generate_code(cfg.totp_secret)
    log.info("generated TOTP code (length=%d); valid for %ds",
             len(code), seconds_until_next_code())

    # Use CSS selectors instead of @eN refs because:
    #   1. Form field names on mailon.kr are stable (ipt-id, ipt-pw, ipt-otp)
    #   2. @eN refs require a fresh snapshot AND can become invalid if
    #      anything modifies the DOM between snapshot and fill
    log.info("filling credentials via CSS selectors")
    browser.fill('input[name="ipt-id"]', cfg.mailon_id)
    browser.fill('input[name="ipt-pw"]', cfg.mailon_pw)
    browser.fill('input[name="ipt-otp"]', code)

    # Submit by calling the page's own login() JS function - this bypasses
    # any visual overlay (popups) that might be blocking the click.
    log.info("submitting login form")
    browser.eval_js("typeof login === 'function' ? (login(), 'ok') : 'no-login-fn'")

    # Wait for navigation away from login page (up to 25s)
    try:
        browser.wait_url("**/mail**", timeout_s=25)
    except BrowserError:
        # Check whether we're still on login page -> credential / OTP failure
        url = browser.current_url()
        if "login" in url:
            # Try to read any error banner
            try:
                body = browser.eval_json(
                    "document.body.innerText.substring(0, 500)"
                )
            except Exception:
                body = ""
            raise LoginError(
                f"login did not leave /integrated/login. url={url} "
                f"snippet={str(body)[:200]!r}"
            )
        # Otherwise we navigated somewhere unexpected but at least off login
        log.info("landed on %s (not /mail, but off login)", url)

    log.info("login succeeded; url=%s", browser.current_url())


def _dismiss_popups(browser: AgentBrowser) -> None:
    """Close any announcement popups that appear on the login page."""
    # Up to 3 popups (mailon sometimes chains them)
    for _ in range(3):
        try:
            # The close button is consistently labeled "닫기"
            browser.find_click("text", "닫기", exact=True)
            browser.wait_ms(400)
        except BrowserError:
            break  # no more popups
