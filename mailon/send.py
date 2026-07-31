from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Protocol

from . import send_trigger, send_verify

log = logging.getLogger(__name__)

# Retry idempotency: a matching mail already in Sent within this window means
# a previous (possibly "failed-to-verify") attempt actually delivered.
DUPLICATE_SUPPRESS_WINDOW_MS = 20 * 60 * 1000
# Sent-folder confirmation window starts this much before beforeSend() fires
# (client/server clock skew allowance).
SENT_CONFIRM_SKEW_MS = 120 * 1000


class SendValidationError(ValueError):
    pass


class SendSafetyError(RuntimeError):
    pass


class ComposeBrowser(Protocol):
    def eval_js(self, script: str) -> str: ...
    def eval_json(self, script: str): ...
    def fill(self, selector: str, value: str) -> None: ...
    def wait_ms(self, milliseconds: int) -> None: ...
    def clear_network_requests(self) -> None: ...
    def network_post_count(self) -> int: ...
    def network_requests(self) -> str: ...


@dataclass(frozen=True)
class SendRequest:
    recipients: tuple[str, ...]
    cc: tuple[str, ...]
    subject: str
    body: str
    attachments: tuple[Path, ...]

    def __post_init__(self) -> None:
        if not self.recipients:
            raise SendValidationError("at least one recipient is required")
        if not self.subject.strip():
            raise SendValidationError("a subject is required")
        if not self.body.strip():
            raise SendValidationError("a body is required")


@dataclass(frozen=True)
class SendResult:
    status: str
    csrf_present: bool
    attachment_count: int
    network_post_count: int
    verified: bool = False

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"), sort_keys=True)


class ComposeSender:
    def __init__(
        self,
        browser: ComposeBrowser,
        *,
        verify_timeout_s: float = 45.0,
        verify_poll_s: float = 3.0,
        fastfail_timeout_s: float = 10.0,
        resolve_timeout_s: float = 90.0,
        clock: Callable[[], float] = time.monotonic,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        self._browser = browser
        self._verify_timeout_s = verify_timeout_s
        self._verify_poll_s = verify_poll_s
        self._fastfail_timeout_s = fastfail_timeout_s
        self._resolve_timeout_s = resolve_timeout_s
        self._clock = clock
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))

    def send(self, request: SendRequest, *, dry_run: bool) -> SendResult:
        folder_uid = ""
        if not dry_run:
            try:
                folder_uid = send_verify.resolve_current_folder_uid(
                    self._browser, timeout_s=self._resolve_timeout_s,
                    poll_interval_s=self._verify_poll_s, clock=self._clock)
                if send_verify.find_mail_match(
                    self._browser, folder_uid, request.subject,
                    request.recipients,
                    self._now_ms() - DUPLICATE_SUPPRESS_WINDOW_MS,
                    timeout_s=0, poll_interval_s=0, clock=self._clock,
                ):
                    log.warning(
                        "duplicate-suppressed: matching mail already in the "
                        "mailbox within window; not sending again")
                    return SendResult(
                        status="submitted", csrf_present=False,
                        attachment_count=len(request.attachments),
                        network_post_count=0, verified=True,
                    )
            except send_verify.SendVerifyError as error:
                raise SendSafetyError(str(error)) from error

        self._browser.eval_js("window._tbar.compose(); 'compose-opened';")
        self._browser.wait_ms(3000)
        self._browser.clear_network_requests()
        compose_metadata = self._browser.eval_json(
            """JSON.stringify((function() {
              var csrf = document.querySelector('#sendCSRFToken');
              var fileInput = document.querySelector('input[type=file]');
              return {csrf: csrf ? {name: csrf.name, value: csrf.value} : null,
                      file_input: fileInput ? '#' + fileInput.id : null};
            })())"""
        )
        csrf = compose_metadata.get("csrf") if isinstance(compose_metadata, dict) else None
        if not csrf:
            raise SendSafetyError("compose page did not expose send CSRF token")

        self._browser.eval_js(_field_fill_script(
            ",".join(request.recipients), ",".join(request.cc), request.subject,
        ))
        self._browser.eval_js(_editor_fill_script(request.body))

        post_count = self._browser.network_post_count()
        if dry_run:
            if post_count:
                raise SendSafetyError(f"dry-run observed {post_count} POST request(s)")
            return SendResult(
                status="dry_run",
                csrf_present=True,
                attachment_count=len(request.attachments),
                network_post_count=0,
                verified=False,
            )

        if request.attachments:
            raise SendSafetyError("attachment upload requires a verified file input")
        send_trigger.commit_recipients(self._browser, request.recipients, request.cc)
        send_trigger.sync_field_events(self._browser)
        log.info("compose discovery: %s", send_trigger.discover_compose(self._browser))
        form_ok, form_dump = send_trigger.verify_compose_form(
            self._browser, request.recipients, request.body)
        if not form_ok:
            log.warning("compose form incomplete (%s); refilling body via editor API",
                        form_dump[:800])
            send_trigger.fill_body_via_editor_api(self._browser, request.body)
            form_ok, form_dump = send_trigger.verify_compose_form(
                self._browser, request.recipients, request.body)
        log.info("compose form state: %s", form_dump[:2500])
        if not form_ok:
            raise SendSafetyError(
                "compose form missing body/recipients (would send garbage) — "
                "form dump logged")
        baseline_ids = send_verify.request_ids(self._browser.network_requests())
        send_started_ms = self._now_ms()
        triggered = (send_trigger.call_compose_send(self._browser)
                     or send_trigger.click_send_button(self._browser))
        if not triggered:
            raise SendSafetyError(
                "no send trigger available (_compose.send missing AND button "
                "not found) — discovery logged; refusing to fire blind")
        self._browser.wait_ms(500)
        send_verify.log_network_forensics(self._browser, baseline_ids)
        try:
            send_verify.check_network_fast_fail(
                self._browser, baseline_ids,
                timeout_s=self._fastfail_timeout_s, clock=self._clock,
            )
            if not send_verify.find_mail_match(
                self._browser, folder_uid, request.subject,
                request.recipients, send_started_ms - SENT_CONFIRM_SKEW_MS,
                timeout_s=self._verify_timeout_s,
                poll_interval_s=self._verify_poll_s, clock=self._clock,
            ):
                raise send_verify.SendVerifyError(
                    "send not confirmed in mailbox (allFolder) within "
                    f"{self._verify_timeout_s:.0f}s")
        except send_verify.SendVerifyError as error:
            raise SendSafetyError(str(error)) from error
        return SendResult(
            status="submitted",
            csrf_present=True,
            attachment_count=0,
            network_post_count=self._browser.network_post_count(),
            verified=True,
        )


def _editor_fill_script(body: str) -> str:
    encoded_body = json.dumps(body)
    return (
        "(function(value) {"
        "var frame = document.querySelector('#NamoSE_Ifr__mail_editor_nm');"
        "if (!frame || !frame.contentDocument || !frame.contentDocument.body) {"
        "throw new Error('compose editor frame unavailable');"
        "}"
        "frame.contentDocument.body.innerText = value;"
        "frame.contentDocument.body.dispatchEvent(new Event('input', {bubbles: true}));"
        f"}})({encoded_body});"
    )


def _field_fill_script(recipients: str, cc: str, subject: str) -> str:
    encoded = json.dumps([recipients, cc, subject])
    return (
        "(function(values) {"
        "['#adr-to-ipt_ta','#adr-cc-ipt_ta','#compose_subject'].forEach(function(selector, index) {"
        "var element = document.querySelector(selector);"
        "if (!element) { throw new Error('compose field unavailable'); }"
        "element.value = values[index];"
        "});"
        f"}})({encoded});"
    )


def record_send_result(logs_dir: Path, result: SendResult) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    with (logs_dir / "send-attempts.jsonl").open("a", encoding="utf-8") as log_file:
        log_file.write(result.to_json() + "\n")
