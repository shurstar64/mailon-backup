"""Offline tests that exercise all modules WITHOUT network or real credentials.

Run:  .venv\\Scripts\\python -m pytest tests/ -v
Or:   .venv\\Scripts\\python -m tests.test_offline  (fallback)

These tests:
  - fake an AgentBrowser using a small stub that records calls
  - verify the login flow fills fields in the right order with the right
    values (real ID/PW/OTP are dummy)
  - verify the scraper correctly parses mock JSON payloads
  - verify Markdown output is byte-exact for a known input
  - verify the full CLI parser works
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

# Make project root importable
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# --------------------------------------------------------------- TOTP

def test_totp_deterministic():
    from mailon.totp import generate_code, verify_code

    secret = "JBSWY3DPEHPK3PXP"
    c1 = generate_code(secret, at=1714000000)
    c2 = generate_code(secret, at=1714000000)
    assert c1 == c2
    assert len(c1) == 6 and c1.isdigit()
    assert verify_code(secret, generate_code(secret))


def test_totp_secret_whitespace_tolerant():
    from mailon.totp import generate_code

    # Google often displays secrets with spaces: "abcd efgh ijkl"
    assert generate_code("JBSWY3DPEHPK3PXP", at=1) == \
           generate_code("JBSW Y3DP EHPK 3PXP", at=1) == \
           generate_code("JBSW-Y3DP-EHPK-3PXP", at=1)


# -------------------------------------------------------------- State

def test_state_roundtrip(tmp_path):
    from mailon.state import StateDB

    db = StateDB(tmp_path / "s.db")
    assert db.message_count() == 0
    db.record_message("u1", folder="inbox", subject="s", sender="x",
                      recv_date="2026-01-01", markdown_path="a.md")
    db.record_message("u2", folder="inbox", subject="s2", sender="y",
                      recv_date="2026-01-02", markdown_path="b.md")
    assert db.existing_uids() == {"u1", "u2"}
    # REPLACE semantics
    db.record_message("u1", folder="inbox", subject="new", sender="x",
                      recv_date="2026-01-01", markdown_path="a.md")
    assert db.message_count() == 2  # still 2, not 3

    rid = db.start_run()
    db.finish_run(rid, status="ok", new_mails=7)


def test_state_attachment_tracking(tmp_path):
    """Attachments table: ok/fail tracking, retry queue, duplicate prevention."""
    from mailon.state import StateDB

    db = StateDB(tmp_path / "s.db")

    # Record first attempt: failure
    db.record_attachment(
        "u1", filename="big.mp4", href="/download?id=1",
        status="fail", size_bytes=547980148,
        error_msg="Failed to fetch",
    )
    assert not db.has_attachment_ok("u1", "big.mp4")
    failed = db.failed_attachments_for("u1")
    assert len(failed) == 1 and failed[0]["filename"] == "big.mp4"
    assert failed[0]["attempts"] == 1

    # Second failure increments attempts
    db.record_attachment(
        "u1", filename="big.mp4", href="/download?id=1",
        status="fail", size_bytes=547980148,
        error_msg="Timeout",
    )
    failed = db.failed_attachments_for("u1")
    assert failed[0]["attempts"] == 2

    # Retry limit respected
    for _ in range(5):
        db.record_attachment(
            "u1", filename="big.mp4", href="/download?id=1",
            status="fail", size_bytes=547980148, error_msg="x",
        )
    assert db.failed_attachments_for("u1", max_attempts=5) == []  # exhausted

    # Successful recovery removes from fail list
    db.record_attachment(
        "u2", filename="ok.pdf", href="/download?id=2",
        status="ok", size_bytes=1000,
        local_path="data/attachments/u2/ok.pdf",
    )
    assert db.has_attachment_ok("u2", "ok.pdf")
    assert not db.has_attachment_ok("u2", "unknown.pdf")
    assert db.failed_attachments_for("u2") == []

    # Stats
    stats = db.attachment_stats()
    assert stats["fail"] >= 1
    assert stats["ok"] == 1


def test_split_filename_and_size():
    """mailon.kr UI quirk: '<filename> <bytes>' trailing size pattern."""
    from mailon.scraper import split_filename_and_size

    # Trailing size stripped
    assert split_filename_and_size("sample_video_v03.mp4 547980148") == (
        "sample_video_v03.mp4", 547980148,
    )
    assert split_filename_and_size("report.pdf 1234567") == (
        "report.pdf", 1234567,
    )
    # Korean filenames with trailing size
    assert split_filename_and_size("회의록 260330.hwp 45056") == (
        "회의록 260330.hwp", 45056,
    )

    # No trailing size: keep intact
    assert split_filename_and_size("file.pdf") == ("file.pdf", None)
    # 3-digit trailing number: NOT stripped (could be legitimate)
    assert split_filename_and_size("v123.pdf") == ("v123.pdf", None)
    assert split_filename_and_size("file 123.pdf") == ("file 123.pdf", None)

    # Empty / whitespace
    assert split_filename_and_size("") == ("", None)
    assert split_filename_and_size("   ") == ("", None)

    # Filename with spaces preserved
    assert split_filename_and_size("메일 현황 보고서.htm 99240") == (
        "메일 현황 보고서.htm", 99240,
    )


def test_parse_view_async_strips_size_from_attachment_name():
    """View HTML with 'name size' pattern yields clean filename + declared_size."""
    from mailon.scraper import parse_view_async_html

    html = """
    <div id="mvw_wrap">
      <div class="hd"><h4>Test Subject</h4></div>
      <div class="bd"><div class="ct">Body</div></div>
      <a href="/mail/download?attachId=1">sample_video_v03.mp4 547980148</a>
      <a href="/mail/download?attachId=2">small.pdf 12345</a>
    </div>
    """
    out = parse_view_async_html(html)
    assert len(out["attachments"]) == 2
    assert out["attachments"][0]["filename"] == "sample_video_v03.mp4"
    assert out["attachments"][0]["declared_size"] == 547980148
    assert out["attachments"][1]["filename"] == "small.pdf"
    assert out["attachments"][1]["declared_size"] == 12345


# ------------------------------------------------------------- Writer

def test_writer_exact_output(tmp_path):
    from mailon.writer import Mail, Attachment, write_mail, path_for, build_markdown

    root = tmp_path
    mails = root / "data" / "mails"
    attach = root / "data" / "attachments"
    attach.mkdir(parents=True)

    # Create a dummy attachment
    att_file = attach / "42" / "report.pdf"
    att_file.parent.mkdir(parents=True)
    att_file.write_bytes(b"%PDF dummy")

    m = Mail(
        uid="42",
        folder="inbox",
        subject="테스트 메일",
        sender="김미미 <me@mailon.kr>",
        to="you@mailon.kr",
        date=datetime(2026, 4, 22, 9, 30, 15),
        body_text="안녕하세요.\n본문 테스트",
        attachments=[Attachment("report.pdf", att_file, size=10)],
    )
    p = write_mail(m, mails, root)
    assert p.exists()
    txt = p.read_text(encoding="utf-8")
    assert "테스트 메일" in txt
    assert 'uid: "42"' in txt
    assert "report.pdf" in txt
    assert "안녕하세요" in txt
    # Relative attachment link uses POSIX slashes.
    # Depth: data/mails/YYYY/MM -> 4 '..' back to root, then data/attachments/...
    assert "../../../../data/attachments/42/report.pdf" in txt


def test_writer_path_sanitization(tmp_path):
    from mailon.writer import Mail, path_for

    m = Mail(uid="99", folder="inbox", subject="subj/with:bad*chars",
             sender="x", date=datetime(2026, 1, 5))
    p = path_for(m, tmp_path)
    # No forbidden chars in filename
    for ch in '\\/:*?"<>|':
        if ch == "\\":
            continue  # path separator on windows
        assert ch not in p.name


# ------------------------------------------------------ Korean dates

def test_date_parser():
    from mailon.scraper import parse_korean_date

    d1 = parse_korean_date("2026.04.21 (화) 14:38")
    assert d1 is not None and d1.strftime("%Y-%m-%d %H:%M") == "2026-04-21 14:38"
    d2 = parse_korean_date("2026-04-22 09:30:15")
    assert d2 is not None and d2.second == 15
    assert parse_korean_date("") is None
    assert parse_korean_date("gibberish") is None


# -------------------------------------------------------- Login flow

def test_login_field_discovery():
    """Simulate AX snapshot with id/pw/otp textboxes; verify mapping."""
    from mailon.login import _find_login_refs

    fake_snapshot = [
        {
            "role": "generic",
            "children": [
                {"role": "textbox", "ref": "e17", "name": "email id"},
                {"role": "textbox", "ref": "e18", "name": "password"},
                {"role": "textbox", "ref": "e19", "placeholder": "OTP"},
                {"role": "button", "ref": "e8", "name": "로그인"},
            ],
        },
    ]

    browser = MagicMock()
    browser.snapshot_json.return_value = fake_snapshot
    refs = _find_login_refs(browser)
    assert refs["otp"] == "e19"  # OTP matched by placeholder text
    assert refs["id"] == "e17"
    assert refs["pw"] == "e18"
    assert refs["submit"] == "e8"


def test_login_flow_calls_in_order():
    """End-to-end login flow with fake browser. Verifies we fill in the right
    order and click submit."""
    from mailon.browser import AgentBrowser
    from mailon.config import Config
    from mailon import login as login_module

    calls = []

    class FakeBrowser(AgentBrowser):
        def __init__(self):
            # Minimal init - skip PATH resolution since we don't actually run CLI
            self.session_name = "test"
            self.headless = True
            self.executable = "agent-browser-stub"
            self.timeout = 30

        def open(self, url, *, timeout=None): calls.append(("open", url))
        def wait_load(self, event="networkidle"): calls.append(("wait_load", event))
        def wait_ms(self, ms): calls.append(("wait_ms", ms))
        def wait_url(self, pattern, timeout_s=25): calls.append(("wait_url", pattern))
        def snapshot_json(self, interactive_only=True):
            return [{
                "role": "generic",
                "children": [
                    {"role": "textbox", "ref": "e17"},
                    {"role": "textbox", "ref": "e18"},
                    {"role": "textbox", "ref": "e19", "placeholder": "OTP"},
                    {"role": "button", "ref": "e8", "name": "로그인"},
                ],
            }]
        def fill(self, ref, value):
            # NEVER log value for fields containing secrets (pw/otp)
            is_id = ref.endswith('ipt-id"]') or ref == "e17"
            calls.append(("fill", ref, value if is_id else "***"))
        def click(self, ref, *, new_tab=False): calls.append(("click", ref))
        def eval_js(self, js):
            # login() calls eval_js('typeof login===...') to submit form
            calls.append(("eval_js", js[:50]))
            return "'ok'"
        def find_click(self, *a, **k):
            raise RuntimeError("no popups")  # pretend no popup to close
        def current_url(self): return "https://mailon.kr/mail/inbox"

    cfg = Config(
        mailon_id="test@mailon.kr",
        mailon_pw="secret",
        totp_secret="JBSWY3DPEHPK3PXP",
        login_url="https://mailon.kr/",
        headless=True,
        max_mails_per_run=0,
        data_dir=Path("."),
        mails_dir=Path("."),
        attachments_dir=Path("."),
        logs_dir=Path("."),
        state_db_path=Path(":memory:"),
    )

    browser = FakeBrowser()
    # Patch BrowserError in login module to match what find_click raises
    from mailon import browser as browser_module
    with patch.object(login_module, "BrowserError", RuntimeError):
        login_module.login(browser, cfg)

    # Verify sequence: open, fill id, fill pw, fill otp, submit via eval_js
    fills = [c for c in calls if c[0] == "fill"]
    assert fills[0] == ("fill", 'input[name="ipt-id"]', "test@mailon.kr")
    assert fills[1][1] == 'input[name="ipt-pw"]'
    assert fills[2][1] == 'input[name="ipt-otp"]'
    assert fills[2][2] == "***"  # we checked pw/otp weren't logged

    # Submit via eval_js (login() JS call)
    evals = [c for c in calls if c[0] == "eval_js"]
    assert any("login" in e[1] for e in evals)

    # URL wait happened
    assert any(c[0] == "wait_url" for c in calls)


# ---------------------------------------------------- Scraper parsing

def test_scraper_mail_list_parse():
    """list_async.json JSON response -> list_inbox extracts MailRef correctly."""
    from mailon.scraper import InboxScraper

    # Mimics /mail/list_async.json response
    page1_resp = json.dumps({
        "result": True,
        "folder": {"folderUid": 10001, "newMsgNum": 2},
        "contents": [
            {"mailUid": 100, "folderUid": 10001, "subject": "Hello",
             "adrFrom": '"A" <a@b.com>', "adrTo": '<me@x.com>',
             "timeMillis": 1776836838000, "isSeen": 0, "isFlagged": 0,
             "msgSize": 1000, "attachCount": 0},
            {"mailUid": 101, "folderUid": 10001, "subject": "Re: World",
             "adrFrom": '"C" <c@d.com>', "adrTo": '<me@x.com>',
             "timeMillis": 1776836000000, "isSeen": 1, "isFlagged": 0,
             "msgSize": 500, "attachCount": 0},
        ],
    })
    # Second page: empty contents → scraper stops
    empty_resp = json.dumps({
        "result": True, "folder": {"folderUid": 10001}, "contents": []
    })

    browser = MagicMock()
    browser.eval_js.side_effect = [page1_resp, empty_resp]

    scraper = InboxScraper(browser, Path("/tmp"))
    scraper.folder_uid = "10001"  # manually set since we skip resolve_inbox_folder_uid()

    # list_inbox stops early because page 1 has only 2 items (< PAGE_SIZE=20)
    refs = scraper.list_inbox()

    assert len(refs) == 2
    assert refs[0].uid == "100" and refs[0].unread is True
    assert refs[0].folder_uid == "10001"
    assert refs[0].sender == '"A" <a@b.com>'
    assert refs[1].subject == "Re: World"
    assert refs[1].unread is False


def test_scraper_read_mail_parse():
    """view_async HTML response -> read_mail builds Mail correctly."""
    from datetime import datetime
    from mailon.scraper import InboxScraper, MailRef

    # Synthetic view_async HTML (mimics mailon.kr's actual response)
    html = """
    <div>
      <input type="hidden" id="mailUid" value="777" />
      <input type="hidden" id="folderUid" value="10001" />
      <input type="hidden" id="timeMillis" value="1776836838000" />
      <div id="mvw_wrap">
        <div class="hd">
          <h4>테스트 메일 제목</h4>
        </div>
        <ul>
          <li id="from-item-0" data-address="sender@x.com"
              data-personal="보낸사람">x</li>
          <li id="to-item-0" data-address="me@mailon.kr"
              data-personal="">y</li>
        </ul>
        <div class="bd">
          <div class="ct">본문 내용 테스트</div>
        </div>
      </div>
    </div>
    """

    browser = MagicMock()
    browser.eval_js.return_value = json.dumps(html)

    ref = MailRef(
        uid="777", folder_uid="10001", subject="fallback-subj",
        sender="fallback-sender", to="", date=datetime(2026, 1, 1),
        size=1000, unread=False, flagged=False, attach_count=0,
    )

    scraper = InboxScraper(browser, Path("/tmp"))
    mail = scraper.read_mail(ref)

    assert mail.uid == "777"
    assert mail.subject == "테스트 메일 제목"
    assert "보낸사람" in mail.sender and "sender@x.com" in mail.sender
    assert mail.date is not None and mail.date.year == 2026
    assert "본문 내용 테스트" in mail.body_text
    assert mail.attachments == []


# -------------------------------------------------------------- CLI

def test_cli_parser_all_commands():
    from mailon.main import build_parser

    p = build_parser()
    # Every documented command parses
    for cmd in ("totp", "login", "probe", "sync", "status"):
        ns = p.parse_args([cmd])
        assert ns.cmd == cmd
    # --limit on sync
    ns = p.parse_args(["sync", "--limit", "3"])
    assert ns.limit == 3


def test_cli_missing_env_message(tmp_path):
    """Running a cmd that needs .env without .env must print a helpful error."""
    # Isolate: run from a clean dir without .env, clear env vars
    import subprocess
    import shutil

    # Copy the mailon package + pyrightconfig into a temp workspace without .env
    pkg_dst = tmp_path / "mailon"
    shutil.copytree(ROOT / "mailon", pkg_dst)

    env = {k: v for k, v in os.environ.items() if not k.startswith("MAILON_")}
    # Ensure PYTHONPATH finds our copy
    env["PYTHONPATH"] = str(tmp_path)
    proc = subprocess.run(
        [sys.executable, "-m", "mailon.main", "login"],
        cwd=tmp_path,
        env=env,
        capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode != 0
    assert "MAILON_" in (proc.stderr + proc.stdout)
    assert ".env" in (proc.stderr + proc.stdout)


class FakeComposeBrowser:
    def __init__(self):
        self.fill_calls = []
        self.javascript_calls = []
        self.post_count = 0
        self.wait_calls = []
        self.network_clear_count = 0

    def eval_js(self, script):
        self.javascript_calls.append(script)
        return "ok"

    def eval_json(self, script):
        return {
            "csrf": {"name": "sendCSRFToken", "value": "fixture-csrf-token"},
            "file_input": "#uploaderAttach",
        }

    def fill(self, selector, value):
        self.fill_calls.append((selector, value))

    def wait_ms(self, milliseconds):
        self.wait_calls.append(milliseconds)
        return None

    def network_post_count(self):
        return self.post_count

    def clear_network_requests(self):
        self.network_clear_count += 1


def test_send_dry_run_maps_compose_fields_without_a_post():
    from mailon.send import ComposeSender, SendRequest

    browser = FakeComposeBrowser()
    request = SendRequest(
        recipients=("recipient@example.test",),
        cc=("copy@example.test",),
        subject="offline subject",
        body="offline body",
        attachments=(),
    )

    result = ComposeSender(browser).send(request, dry_run=True)

    field_fill = next(script for script in browser.javascript_calls if "#compose_subject" in script)
    assert "#adr-to-ipt_ta" in field_fill
    assert "#adr-cc-ipt_ta" in field_fill
    assert "offline subject" in field_fill
    assert result.status == "dry_run"
    assert result.csrf_present is True
    assert result.network_post_count == 0
    assert browser.wait_calls == [3000]
    assert browser.network_clear_count == 1
    assert all("beforeSend" not in script for script in browser.javascript_calls)


def test_send_rejects_missing_required_fields():
    import pytest

    from mailon.send import SendRequest, SendValidationError

    with pytest.raises(SendValidationError):
        SendRequest(
            recipients=(),
            cc=(),
            subject="offline subject",
            body="offline body",
            attachments=(),
        )

    with pytest.raises(SendValidationError):
        SendRequest(
            recipients=("recipient@example.test",),
            cc=(),
            subject="",
            body="offline body",
            attachments=(),
        )


def test_send_result_json_has_stable_machine_contract():
    import json

    from mailon.send import SendResult

    payload = json.loads(SendResult(
        status="dry_run",
        csrf_present=True,
        attachment_count=0,
        network_post_count=0,
    ).to_json())

    assert payload == {
        "status": "dry_run",
        "csrf_present": True,
        "attachment_count": 0,
        "network_post_count": 0,
        "verified": False,
    }


def test_browser_eval_json_unwraps_agent_browser_json_string():
    from mailon.browser import AgentBrowser

    browser = AgentBrowser.__new__(AgentBrowser)
    browser.eval_js = lambda script: json.dumps(json.dumps({"csrf": {"name": "sendCSRFToken"}}))

    assert browser.eval_json("fixture") == {"csrf": {"name": "sendCSRFToken"}}

# ------------------------------------------- send verification (fail-closed)

NOW_MS = 1_784_500_000_000
SENT_FOLDER_UID = "49999"
VERIFY_SUBJECT = "verify subject"
VERIFY_RECIPIENT = "recipient@example.test"

BASELINE_LISTING = (
    "[11] GET https://mailon.kr/mail (document) 200\n"
    "[15] POST https://mailon.kr/mail/list_async.json (xhr) 200"
)
SEND_OK_LISTING = (
    BASELINE_LISTING + "\n[16] POST https://mailon.kr/mail/send_mail.json (xhr) 200"
)
SEND_FAIL_LISTING = (
    BASELINE_LISTING + "\n[16] POST https://mailon.kr/mail/send_mail.json (xhr) 500"
)

NO_MATCH_PAGE = {"result": True, "contents": []}
MATCH_PAGE = {
    "result": True,
    "contents": [{
        "mailUid": 900, "subject": VERIFY_SUBJECT,
        "adrTo": f"<{VERIFY_RECIPIENT}>", "timeMillis": NOW_MS - 5_000,
    }],
}


class VerifyFakeBrowser(FakeComposeBrowser):
    """FakeComposeBrowser + scriptable verification surfaces."""

    def __init__(self, *, row_uid=SENT_FOLDER_UID, send_call_result="default",
                 click_result="default", form_dumps="default",
                 list_responses=(), network_listings=()):
        super().__init__()
        self.row_uid_seq = row_uid if isinstance(row_uid, list) else [row_uid]
        self.send_call_result = ({"called": "_compose.send()"}
                                 if send_call_result == "default" else send_call_result)
        self.click_result = ({"clicked": "보내기", "scope": "form"}
                             if click_result == "default" else click_result)
        self.form_dumps = ([{"method": "send", "to": f"<{VERIFY_RECIPIENT}>",
                             "from": "me@example.com", "content": "verify body"}]
                           if form_dumps == "default" else list(form_dumps))
        self.editor_api_fills = 0
        self.list_responses = list(list_responses)
        self.network_listings = list(network_listings)
        self.list_call_count = 0
        self.row_harvest_count = 0
        self.request_detail = ""

    def eval_js(self, script):
        self.javascript_calls.append(script)
        if "getForm()" in script and "probe" in script:  # compose form probe
            dump = (self.form_dumps.pop(0) if len(self.form_dumps) > 1
                    else self.form_dumps[0])
            return json.dumps(json.dumps(dump))
        if "refill:" in script:  # body refill into the getForm field
            self.editor_api_fills += 1
            return json.dumps("editor-fill:ok")
        if "typeof c.send" in script:  # direct _compose.send() trigger
            payload = (self.send_call_result if self.send_call_result
                       else {"called": None, "reason": "no _compose.send"})
            return json.dumps(json.dumps(payload))
        if "MouseEvent" in script:  # send-button click fallback
            payload = self.click_result if self.click_result else {"clicked": None}
            return json.dumps(json.dumps(payload))
        if "HTMLTextAreaElement" in script:  # field event sync
            return "events:3"
        if "compose_keys" in script:  # discovery dump
            return json.dumps(json.dumps({"compose_keys": ["beforeSend"]}))
        if "mail-metadata" in script and "folderUid" in script:
            self.row_harvest_count += 1
            uid = (self.row_uid_seq.pop(0) if len(self.row_uid_seq) > 1
                   else self.row_uid_seq[0])
            return json.dumps(uid) if uid else "null"
        if "list_async.json" in script:
            self.list_call_count += 1
            payload = (self.list_responses.pop(0) if len(self.list_responses) > 1
                       else self.list_responses[0])
            return json.dumps(json.dumps(payload))
        if "performance" in script:
            return json.dumps(json.dumps({"url": "fixture", "rows": 0}))
        return "ok"

    def network_requests(self):
        if len(self.network_listings) > 1:
            return self.network_listings.pop(0)
        return self.network_listings[0]

    def network_request_detail(self, request_id):
        return self.request_detail


def _verify_sender(browser):
    from mailon.send import ComposeSender

    return ComposeSender(
        browser, verify_timeout_s=0, verify_poll_s=0, fastfail_timeout_s=0,
        resolve_timeout_s=0, now_ms=lambda: NOW_MS,
    )


def _verify_request():
    from mailon.send import SendRequest

    return SendRequest(
        recipients=(VERIFY_RECIPIENT,), cc=(),
        subject=VERIFY_SUBJECT, body="verify body", attachments=(),
    )


def test_send_success_requires_sent_folder_confirmation():
    browser = VerifyFakeBrowser(
        list_responses=[NO_MATCH_PAGE, MATCH_PAGE],
        network_listings=[BASELINE_LISTING, SEND_OK_LISTING],
    )
    browser.post_count = 1

    result = _verify_sender(browser).send(_verify_request(), dry_run=False)

    assert result.status == "submitted"
    assert result.verified is True
    assert any("typeof c.send" in s for s in browser.javascript_calls)  # send() called
    assert all("MouseEvent" not in s for s in browser.javascript_calls)  # no fallback needed
    assert all("beforeSend();" not in s for s in browser.javascript_calls)
    assert browser.list_call_count == 2  # pre-check + post-send verify
    assert all("allFolder=true" in s for s in browser.javascript_calls
               if "list_async.json" in s)  # verify searches ALL folders


def test_send_fails_closed_when_sent_folder_never_shows_mail():
    import pytest

    from mailon.send import SendSafetyError

    browser = VerifyFakeBrowser(
        list_responses=[NO_MATCH_PAGE],
        network_listings=[BASELINE_LISTING, SEND_OK_LISTING],
    )

    with pytest.raises(SendSafetyError):
        _verify_sender(browser).send(_verify_request(), dry_run=False)

    # the send WAS attempted (_compose.send() called) but never confirmed
    assert any("typeof c.send" in s for s in browser.javascript_calls)


def test_send_precheck_suppresses_duplicate_without_sending():
    browser = VerifyFakeBrowser(
        list_responses=[{
            "result": True,
            "contents": [{
                "mailUid": 901, "subject": VERIFY_SUBJECT,
                "adrTo": f"\"수신자\" <{VERIFY_RECIPIENT}>",
                "timeMillis": NOW_MS - 60_000,
            }],
        }],
        network_listings=[BASELINE_LISTING],
    )

    result = _verify_sender(browser).send(_verify_request(), dry_run=False)

    assert result.status == "submitted"
    assert result.verified is True
    assert all("typeof c.send" not in s for s in browser.javascript_calls)
    assert all("MouseEvent" not in s for s in browser.javascript_calls)
    assert all("_tbar.compose" not in s for s in browser.javascript_calls)


def test_send_fails_fast_on_error_status_post():
    import pytest

    from mailon.send import SendSafetyError

    browser = VerifyFakeBrowser(
        list_responses=[NO_MATCH_PAGE],
        network_listings=[BASELINE_LISTING, SEND_FAIL_LISTING],
    )

    with pytest.raises(SendSafetyError, match="500"):
        _verify_sender(browser).send(_verify_request(), dry_run=False)

    # failed before any post-send Sent-folder polling (only the pre-check ran)
    assert browser.list_call_count == 1


def test_send_fails_when_no_new_post_after_beforesend():
    import pytest

    from mailon.send import SendSafetyError

    # watermark: the pre-check list_async POST ([15]) exists in the baseline;
    # after beforeSend the listing is UNCHANGED -> no new send POST
    browser = VerifyFakeBrowser(
        list_responses=[NO_MATCH_PAGE],
        network_listings=[BASELINE_LISTING, BASELINE_LISTING],
    )

    with pytest.raises(SendSafetyError):
        _verify_sender(browser).send(_verify_request(), dry_run=False)


def test_send_fails_closed_when_no_trigger_available():
    import pytest

    from mailon.send import SendSafetyError

    browser = VerifyFakeBrowser(
        send_call_result=None,  # _compose.send missing
        click_result=None,      # and no send button found
        list_responses=[NO_MATCH_PAGE],
        network_listings=[BASELINE_LISTING],
    )

    with pytest.raises(SendSafetyError, match="trigger"):
        _verify_sender(browser).send(_verify_request(), dry_run=False)

    # nothing was sent and no post-send verification ran (pre-check only)
    assert browser.list_call_count == 1


def test_send_falls_back_to_button_click_when_compose_send_missing():
    browser = VerifyFakeBrowser(
        send_call_result=None,  # _compose.send missing -> button fallback
        list_responses=[NO_MATCH_PAGE, MATCH_PAGE],
        network_listings=[BASELINE_LISTING, SEND_OK_LISTING],
    )
    browser.post_count = 1

    result = _verify_sender(browser).send(_verify_request(), dry_run=False)

    assert result.status == "submitted"
    assert any("MouseEvent" in s for s in browser.javascript_calls)


def test_send_refills_body_via_editor_api_when_form_missing_body():
    """2026-07-19 real bug: getForm() returned body=undefined -> mail sent with
    literal 'undefined' text. Now: probe form, refill via editor API, recheck."""
    browser = VerifyFakeBrowser(
        form_dumps=[
            {"method": "send", "to": f"<{VERIFY_RECIPIENT}>",
             "from": "me@example.com", "content": "undefined"},
            {"method": "send", "to": f"<{VERIFY_RECIPIENT}>",
             "from": "me@example.com", "content": "verify body"},
        ],
        list_responses=[NO_MATCH_PAGE, MATCH_PAGE],
        network_listings=[BASELINE_LISTING, SEND_OK_LISTING],
    )
    browser.post_count = 1

    result = _verify_sender(browser).send(_verify_request(), dry_run=False)

    assert result.status == "submitted"
    assert browser.editor_api_fills == 1  # body was re-committed via editor API


def test_send_fails_closed_when_form_never_contains_body():
    import pytest

    from mailon.send import SendSafetyError

    browser = VerifyFakeBrowser(
        form_dumps=[{"method": "send", "to": f"<{VERIFY_RECIPIENT}>",
                     "from": "me@example.com", "content": "undefined"}],
        list_responses=[NO_MATCH_PAGE],
        network_listings=[BASELINE_LISTING],
    )

    with pytest.raises(SendSafetyError, match="form"):
        _verify_sender(browser).send(_verify_request(), dry_run=False)

    # never triggered the actual send with a garbage body
    assert all("typeof c.send" not in s for s in browser.javascript_calls)


def test_send_fails_closed_on_tome_redirect_mismatch():
    """method=='tome' makes send() replace to with from; refuse when our
    recipients are not the account itself (mail would silently self-redirect)."""
    import pytest

    from mailon.send import SendSafetyError

    browser = VerifyFakeBrowser(
        form_dumps=[{"method": "tome", "to": f"<{VERIFY_RECIPIENT}>",
                     "from": "someone-else@example.com", "content": "verify body"}],
        list_responses=[NO_MATCH_PAGE],
        network_listings=[BASELINE_LISTING],
    )

    with pytest.raises(SendSafetyError, match="form"):
        _verify_sender(browser).send(_verify_request(), dry_run=False)


def test_resolve_current_folder_waits_for_late_row_render():
    """MailOn SPA renders the mail list seconds after login: resolve must poll."""
    from mailon import send_verify

    browser = VerifyFakeBrowser(row_uid=[None, None, "10001"])

    uid = send_verify.resolve_current_folder_uid(
        browser, timeout_s=30, poll_interval_s=0)

    assert uid == "10001"
    assert browser.row_harvest_count == 3  # kept retrying until rows rendered


def test_resolve_current_folder_fails_closed_with_page_diagnostics():
    import pytest

    from mailon import send_verify

    browser = VerifyFakeBrowser(row_uid=[None])

    with pytest.raises(send_verify.SendVerifyError):
        send_verify.resolve_current_folder_uid(
            browser, timeout_s=0, poll_interval_s=0)

    assert browser.row_harvest_count >= 1
    # page diagnostics were captured for the post-mortem
    assert any("performance" in s for s in browser.javascript_calls)


# ------------------------------------------------- sent-folder resolution

INBOX_UID = "10001"
ACCOUNT = "testuser@example.com"


def _row(folder_uid, adr_from, adr_to, uid=1):
    return {"mailUid": uid, "folderUid": folder_uid, "adrFrom": adr_from,
            "adrTo": adr_to, "subject": "s", "timeMillis": 1784500000000}


class FolderFakeBrowser:
    def __init__(self, *, harvest_result=None, pages=()):
        self.harvest_result = harvest_result
        self.pages = list(pages)
        self.list_calls = 0

    def eval_js(self, script):
        if "contentDocument" in script:  # cross-frame sidebar harvest
            return json.dumps(self.harvest_result) if self.harvest_result else "null"
        if "list_async.json" in script:
            self.list_calls += 1
            page = (self.pages[self.list_calls - 1]
                    if self.list_calls <= len(self.pages)
                    else {"result": True, "contents": []})
            return json.dumps(json.dumps(page))
        return "null"


def test_resolve_folder_uid_prefers_iframe_harvest():
    from mailon.folders import resolve_folder_uid

    browser = FolderFakeBrowser(harvest_result="60001")

    uid = resolve_folder_uid(browser, "보낸메일함",
                             inbox_uid=INBOX_UID, account_email=ACCOUNT)

    assert uid == "60001"
    assert browser.list_calls == 0  # no inference needed


def test_resolve_folder_uid_infers_from_allfolder_rows():
    from mailon.folders import resolve_folder_uid

    me = f"\"홍길동\" <{ACCOUNT}>"
    page = {"result": True, "contents": [
        _row(INBOX_UID, "other@x.com", f"<{ACCOUNT}>", 1),      # inbox row
        _row("60001", me, "<someone@else.com>", 2),             # sent → counts
        _row("60001", me, "<another@x.com>", 3),                # sent → counts
        _row("60002", me, f"<{ACCOUNT}>", 4),                   # tome → excluded
        _row("60002", me, f"<{ACCOUNT}>", 5),                   # tome → excluded
        _row(INBOX_UID, me, "<x@y.com>", 6),                    # inbox uid → excluded
    ]}

    browser = FolderFakeBrowser(harvest_result=None, pages=[page])

    uid = resolve_folder_uid(browser, "보낸메일함",
                             inbox_uid=INBOX_UID, account_email=ACCOUNT)

    assert uid == "60001"


def test_resolve_folder_uid_fails_open_when_inconclusive():
    from mailon.folders import resolve_folder_uid

    me = f"<{ACCOUNT}>"
    single_hit = {"result": True, "contents": [
        _row("60001", me, "<someone@else.com>", 1),  # only ONE hit -> <2 threshold
    ]}

    browser = FolderFakeBrowser(harvest_result=None, pages=[single_hit])

    assert resolve_folder_uid(browser, "보낸메일함",
                              inbox_uid=INBOX_UID, account_email=ACCOUNT) is None


def test_scraper_folder_label_flows_into_mail():
    from datetime import datetime
    from mailon.scraper import InboxScraper, MailRef

    browser = MagicMock()
    browser.eval_js.return_value = json.dumps(
        '<div><div id="mvw_wrap"><div class="hd"><h4>Sent subject</h4></div>'
        '<div class="bd"><div class="ct">sent body</div></div></div></div>')

    ref = MailRef(uid="801", folder_uid="60001", subject="fb", sender="me",
                  to="you", date=datetime(2026, 7, 20), size=1, unread=False,
                  flagged=False, attach_count=0)

    scraper = InboxScraper(browser, Path("/tmp"), folder_label="sent")
    mail = scraper.read_mail(ref)

    assert mail.folder == "sent"
    assert mail.subject == "Sent subject"



# ---------------------------------------------------------- runnable

if __name__ == "__main__":
    # Poor man's test runner (works even without pytest)
    import inspect

    # Force UTF-8 console on Windows so Korean / unicode crashes don't
    # obscure real test failures.
    if sys.platform == "win32":
        for _s in (sys.stdout, sys.stderr):
            _r = getattr(_s, "reconfigure", None)
            if callable(_r):
                try:
                    _r(encoding="utf-8", errors="replace")
                except Exception:
                    pass

    local = dict(globals())
    tests = [(n, f) for n, f in local.items()
             if n.startswith("test_") and callable(f)]
    fails = 0
    for name, fn in tests:
        sig = inspect.signature(fn)
        try:
            if "tmp_path" in sig.parameters:
                with tempfile.TemporaryDirectory() as td:
                    fn(Path(td))
            else:
                fn()
            print(f"PASS  {name}")
        except AssertionError as e:
            fails += 1
            print(f"FAIL  {name}: {e}")
        except Exception as e:
            fails += 1
            print(f"ERROR {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - fails}/{len(tests)} passed")
    sys.exit(1 if fails else 0)


# ------------------------------------------------- sent-folder sync (main)

def test_sync_folder_records_sent_mails_with_collision_guard(tmp_path):
    from datetime import datetime
    from mailon.main import _sync_folder
    from mailon.state import StateDB
    from mailon.writer import Mail

    db = StateDB(tmp_path / "s.db")
    # uid 901 already exists under INBOX (global-PK collision case)
    db.record_message("901", folder="inbox", subject="old", sender="x",
                      recv_date="2026-07-19", markdown_path="a.md")

    mails = [
        Mail(uid="901", folder="sent", subject="collide", sender="me",
             date=datetime(2026, 7, 20), body_text="b1"),
        Mail(uid="902", folder="sent", subject="fresh", sender="me",
             date=datetime(2026, 7, 20), body_text="b2"),
    ]
    scraper = MagicMock()
    scraper.folder_label = "sent"
    scraper.iter_new_mails.return_value = iter(mails)

    cfg = MagicMock()
    cfg.mails_dir = tmp_path / "mails"

    new_count = _sync_folder(db, scraper, cfg, 0, tmp_path)

    assert new_count == 1                                   # only 902 recorded
    assert db.existing_uids(folder="sent") == {"902"}
    assert db.existing_uids(folder="inbox") == {"901"}      # inbox row intact
    # collision mail still written as markdown (usable), just not DB-recorded
    written = list((tmp_path / "mails").rglob("*.md"))
    assert len(written) == 2


def test_parse_folders_validates_tokens():
    import pytest

    from mailon.main import _parse_folders

    assert _parse_folders("inbox,sent") == ["inbox", "sent"]
    assert _parse_folders("sent") == ["sent"]
    with pytest.raises(ValueError):
        _parse_folders("bogus")
    with pytest.raises(ValueError):
        _parse_folders("")


def test_sync_summary_line_keeps_wrapper_contract():
    import re

    from mailon.main import _sync_summary_line

    line = _sync_summary_line(7, 1, 2)
    # skills/mail/scripts/mailon_interface.py: r"^OK: (?P<new>\d+) new mail\(s\)"
    assert re.match(r"^OK: (?P<new>\d+) new mail\(s\)", line)
    assert re.match(r"^OK: (?P<new>\d+) new mail\(s\)", line).group("new") == "7"


def test_cli_sync_folders_flag():
    from mailon.main import build_parser

    p = build_parser()
    assert p.parse_args(["sync"]).folders == "inbox,sent"
    assert p.parse_args(["sync", "--folders", "sent"]).folders == "sent"


# --------------------------------------- multi-recipient + session isolation

def test_send_tokenizes_each_recipient_individually():
    """3-recipient regression 2026-07-20: comma-joined fill never registered
    chips; each address must be committed with its own Enter tokenization."""
    from mailon.send import SendRequest

    multi_match = {"result": True, "contents": [{
        "mailUid": 950, "subject": VERIFY_SUBJECT,
        "adrTo": "<a@example.com>, <b@example.com>, <c@example.com>",
        "timeMillis": NOW_MS - 5_000,
    }]}
    browser = VerifyFakeBrowser(
        list_responses=[NO_MATCH_PAGE, multi_match],
        network_listings=[BASELINE_LISTING, SEND_OK_LISTING],
    )
    browser.post_count = 1

    request = SendRequest(
        recipients=("a@example.com", "b@example.com", "c@example.com"),
        cc=(), subject=VERIFY_SUBJECT, body="verify body", attachments=(),
    )
    # form dump must contain every recipient for the gate to pass
    browser.form_dumps = [{
        "method": "send", "content": "verify body",
        "to": "a@example.com,b@example.com,c@example.com",
        "from": "me@example.com",
    }]

    result = _verify_sender(browser).send(request, dry_run=False)

    assert result.status == "submitted"
    tokenize_calls = [s for s in browser.javascript_calls if "KeyboardEvent" in s]
    assert len(tokenize_calls) == 1  # one batch call that loops all addresses
    assert "a@example.com" in tokenize_calls[0]
    assert "c@example.com" in tokenize_calls[0]


def test_send_browser_uses_isolated_session():
    """send must NOT share the sync browser session (2026-07-20: concurrent
    send killed a running sent-folder sync sharing session mailon-sync)."""
    from mailon.main import _make_send_browser

    class Cfg:
        session_name = "mailon-sync"
        headless = True

    browser = _make_send_browser(Cfg())
    assert browser.session_name == "mailon-sync-send"


def test_split_addresses_normalizes_comma_joined_values():
    """triage passes ONE --to "a, b, c" string; mailon must split it (2026-07-20)."""
    from mailon.main import _split_addresses

    assert _split_addresses(["a@x.com, b@x.com,c@x.com"]) == (
        "a@x.com", "b@x.com", "c@x.com")
    assert _split_addresses(["a@x.com", "b@x.com"]) == ("a@x.com", "b@x.com")
    assert _split_addresses(["a@x.com; b@x.com"]) == ("a@x.com", "b@x.com")
    assert _split_addresses([]) == ()


def test_mail_match_accepts_first_recipient_only_adrto():
    """MailOn list view shows ONLY the first recipient for multi-recipient
    mails (2026-07-20: sent mail visible at top yet ALL-matching missed it)."""
    from mailon.send_verify import _mail_matches

    item = {"subject": VERIFY_SUBJECT,
            "adrTo": "<a@example.com>",  # first recipient only
            "timeMillis": NOW_MS - 5_000}
    assert _mail_matches(item, VERIFY_SUBJECT,
                         ("a@example.com", "b@example.com", "c@example.com"),
                         NOW_MS - 120_000)
    # unrelated recipients must still NOT match
    assert not _mail_matches(item, VERIFY_SUBJECT,
                             ("zz@other.com",), NOW_MS - 120_000)


def test_resolve_inbox_folder_uid_waits_for_late_rows():
    """Sync-path regression 2026-07-20: one-shot row harvest fails on slow
    MailOn renders; must poll like the send path does."""
    from mailon.scraper import InboxScraper

    browser = MagicMock()
    browser.eval_js.side_effect = ["null", "null", json.dumps("10001")]
    browser.find_click.side_effect = RuntimeError("no sidebar")

    scraper = InboxScraper(browser, Path("/tmp"))
    with patch("mailon.scraper.BrowserError", RuntimeError):
        uid = scraper.resolve_inbox_folder_uid(timeout_s=30, poll_interval_s=0)

    assert uid == "10001"
    assert browser.eval_js.call_count == 3


def test_resolve_inbox_folder_uid_fails_after_deadline():
    import pytest

    from mailon.scraper import InboxScraper

    browser = MagicMock()
    browser.eval_js.return_value = "null"
    browser.find_click.side_effect = RuntimeError("no sidebar")

    scraper = InboxScraper(browser, Path("/tmp"))
    with patch("mailon.scraper.BrowserError", RuntimeError):
        with pytest.raises(RuntimeError, match="folderUid"):
            scraper.resolve_inbox_folder_uid(timeout_s=0, poll_interval_s=0)


# --------------------------------------- recipient resolution (autocomplete)

def test_parse_candidates_organization_cell():
    """Crinity autocomplete grid cell with Organization prefix + :: org suffix."""
    from mailon.resolve import parse_candidates

    cells = [
        'Organization\n"김샘플" <ksample@example.invalid>\n:: 연구지원팀',
    ]
    out = parse_candidates(cells, "김샘플")

    assert len(out) == 1
    c = out[0]
    assert c.group == "organization"
    assert c.name == "김샘플"
    assert c.email == "ksample@example.invalid"
    assert c.org == "연구지원팀"


def test_parse_candidates_contacts_cell_has_empty_org():
    from mailon.resolve import parse_candidates

    out = parse_candidates(['Contacts "김샘플" <ksample@example.invalid>'],
                           "김샘플")

    assert len(out) == 1
    assert out[0].group == "contacts"
    assert out[0].name == "김샘플"
    assert out[0].email == "ksample@example.invalid"
    assert out[0].org == ""


def test_parse_candidates_history_cell_with_org_suffix():
    from mailon.resolve import parse_candidates

    cells = [
        '대표 도메인 "김샘플" <Vibration@example.invalid>'
        ' :: 예시연구원 - 연구지원팀',
    ]
    out = parse_candidates(cells, "김샘플")

    assert len(out) == 1
    assert out[0].group == "history"
    assert out[0].name == "김샘플"
    assert out[0].email == "Vibration@example.invalid"
    assert out[0].org == "예시연구원 - 연구지원팀"


def test_parse_candidates_ignores_placeholder_and_patternless_cells():
    from mailon.resolve import parse_candidates

    cells = [
        "There is no content to be displayed.",
        "연구지원팀",              # no "name" <email> pattern
        "김샘플 ksample@example.invalid",  # unquoted / no angle brackets
        "",
    ]

    assert parse_candidates(cells, "김샘플") == ()


def test_parse_candidates_dedupes_preserving_first_seen_order():
    from mailon.resolve import parse_candidates

    cells = [
        'Contacts "김샘플" <ksample@example.invalid>',
        'Organization\n"김샘플" <ksample2@example.invalid>\n:: 연구지원팀',
        'Contacts "김샘플" <ksample@example.invalid>',   # dup (name,email,group)
    ]
    out = parse_candidates(cells, "김샘플")

    assert len(out) == 2
    assert out[0].group == "contacts"
    assert out[0].email == "ksample@example.invalid"
    assert out[1].group == "organization"
    assert out[1].email == "ksample2@example.invalid"


def test_parse_candidates_empty_input_returns_empty_tuple():
    from mailon.resolve import parse_candidates

    out = parse_candidates([], "김샘플")

    assert out == ()
    assert isinstance(out, tuple)


def test_parse_candidates_unlabeled_cell_maps_to_unknown_group():
    from mailon.resolve import parse_candidates

    out = parse_candidates(['"김샘플" <ksample@example.invalid>'], "김샘플")

    assert len(out) == 1
    assert out[0].group == "unknown"
    assert out[0].name == "김샘플"
    assert out[0].email == "ksample@example.invalid"
    assert out[0].org == ""


def test_parse_candidates_candidate_is_frozen_dataclass():
    import dataclasses

    import pytest

    from mailon.resolve import Candidate

    c = Candidate(group="contacts", name="김샘플",
                  email="ksample@example.invalid", org="")
    assert dataclasses.is_dataclass(c)
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.name = "다른이름"  # type: ignore[misc]
