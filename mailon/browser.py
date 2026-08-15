"""Thin Python wrapper around the `agent-browser` CLI.

All browser interaction in this project goes through this module so that
logging, error handling, and retries are consistent.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path


log = logging.getLogger(__name__)


def _resolve_agent_browser() -> str:
    """Find the agent-browser executable across platforms.

    On Windows, `npm i -g` installs `.cmd` wrappers, not `.exe`. Python's
    subprocess doesn't auto-resolve those unless shell=True. We use
    shutil.which which respects PATHEXT, so .cmd/.bat are found.
    """
    # Explicit override
    override = os.environ.get("AGENT_BROWSER_BIN")
    if override and Path(override).exists():
        return override

    # shutil.which respects PATHEXT on Windows (.cmd, .bat, etc.)
    found = shutil.which("agent-browser")
    if found:
        return found

    # Fallback: check known npm global install location on Windows
    if sys.platform == "win32":
        for candidate in (
            Path(os.environ.get("APPDATA", "")) / "npm" / "agent-browser.cmd",
            Path(os.environ.get("APPDATA", "")) / "npm" / "agent-browser.bat",
            Path(os.environ.get("APPDATA", "")) / "npm" / "agent-browser",
        ):
            if candidate.exists():
                return str(candidate)

    # Give up: return plain name so error message is clearer downstream
    return "agent-browser"


class BrowserError(RuntimeError):
    pass


class AgentBrowser:
    """Wraps the `agent-browser` CLI with a persistent session.

    The browser daemon stays alive across calls (agent-browser design),
    and `--session` isolates this project from other agent-browser usage.
    """

    def __init__(
        self,
        session_name: str = "mailon-sync",
        headless: bool = True,
        executable: str | None = None,
        timeout: int = 90,
        cdp_port: int | None = None,
    ) -> None:
        self.session_name = session_name
        self.headless = headless
        self.executable = executable or _resolve_agent_browser()
        self.timeout = timeout
        # CDP port to connect to an existing Chrome instance
        self.cdp_port = cdp_port or int(os.environ.get("AGENT_BROWSER_CDP_PORT", 0)) or None
        log.debug("agent-browser executable: %s", self.executable)
        if self.cdp_port:
            log.debug("agent-browser CDP port: %d", self.cdp_port)

    # ---------------------------------------------------------------- core

    def _run(self, args: list[str], *, input_text: str | None = None,
             timeout_override: int | None = None) -> str:
        """Run `agent-browser` with common flags; return stdout as str.

        IMPLEMENTATION NOTE (Windows): npm installs agent-browser as a
        `.cmd` wrapper. Invoking a .cmd via subprocess with a list and no
        shell hangs indefinitely on Windows (cmd.exe hooks into stdin
        strangely). We must set shell=True on Windows, which requires
        careful quoting. To stay safe, we pass the command as a single
        string that we build ourselves with proper quoting.
        """
        cmd_parts = [self.executable, "--session", self.session_name]
        if self.cdp_port:
            cmd_parts.extend(["--cdp", str(self.cdp_port)])
        if not self.headless:
            cmd_parts.append("--headed")
        cmd_parts.extend(args)

        use_shell = sys.platform == "win32"
        if use_shell:
            # Quote each part that contains spaces with double quotes.
            # agent-browser args usually don't have special shell chars,
            # but URL paths might contain & etc. We rely on double-quote
            # wrapping.
            quoted = []
            for p in cmd_parts:
                if not p:
                    quoted.append('""')
                elif any(ch in p for ch in ' \t&|<>^"'):
                    # Escape embedded double quotes by doubling them
                    esc = p.replace('"', '""')
                    quoted.append(f'"{esc}"')
                else:
                    quoted.append(p)
            command = " ".join(quoted)
            run_args: list | str = command
        else:
            run_args = cmd_parts

        log.debug("agent-browser %s", " ".join(args))
        timeout = timeout_override if timeout_override is not None else self.timeout

        # On Windows, `subprocess.run(..., capture_output=True)` DEADLOCKS on
        # some agent-browser commands because:
        #   - The child emits a lot on stdout+stderr (~thousands of bytes)
        #   - `run()` waits for the process to exit before reading pipes
        #   - Once pipe buffer (~4-8KB on Windows) fills, child blocks on
        #     write, parent blocks waiting for exit → classic deadlock.
        #
        # Solution: use Popen and drain both pipes with background threads
        # WHILE the child runs. This matches what `subprocess.communicate()`
        # does internally, but `communicate()` itself has the same deadlock
        # issue on Windows with certain shell invocations.
        try:
            proc = subprocess.Popen(
                run_args,
                stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=use_shell,
                bufsize=1,  # line-buffered
            )
        except FileNotFoundError as e:
            raise BrowserError(
                f"'{self.executable}' not found on PATH. "
                "Install with: npm i -g agent-browser && agent-browser install"
            ) from e

        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []

        def _drain(stream, sink):
            try:
                for line in iter(stream.readline, ""):
                    if not line:
                        break
                    sink.append(line)
            except Exception:
                pass
            finally:
                try:
                    stream.close()
                except Exception:
                    pass

        import threading
        t_out = threading.Thread(target=_drain, args=(proc.stdout, stdout_chunks),
                                 daemon=True)
        t_err = threading.Thread(target=_drain, args=(proc.stderr, stderr_chunks),
                                 daemon=True)
        t_out.start()
        t_err.start()

        # Send stdin if any
        if input_text is not None and proc.stdin is not None:
            try:
                proc.stdin.write(input_text)
                proc.stdin.close()
            except Exception as e:
                log.warning("failed to write stdin: %s", e)

        try:
            returncode = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired as e:
            try:
                proc.kill()
            except Exception:
                pass
            t_out.join(timeout=2)
            t_err.join(timeout=2)
            raise BrowserError(
                f"agent-browser timed out after {timeout}s: {' '.join(args)}"
            ) from e

        # Drain any remaining output
        t_out.join(timeout=5)
        t_err.join(timeout=5)

        stdout = "".join(stdout_chunks)
        stderr = "".join(stderr_chunks)

        if returncode != 0:
            raise BrowserError(
                f"agent-browser failed ({returncode}): "
                f"cmd={' '.join(args)} stderr={stderr.strip()}"
            )
        return stdout

    # ---------------------------------------------------------- navigation

    def open(self, url: str, timeout: int = 180) -> None:
        """Open a URL. First call of a session can take 30-120s to launch
        Chrome + daemon, so use a generous timeout here."""
        self._run(["open", url], timeout_override=timeout)

    def close(self) -> None:
        try:
            self._run(["close"])
        except BrowserError as e:
            log.warning("close failed (non-fatal): %s", e)

    def current_url(self) -> str:
        return self._run(["get", "url"]).strip()

    def title(self) -> str:
        return self._run(["get", "title"]).strip()

    # -------------------------------------------------------- interactions

    def snapshot(self, interactive_only: bool = True) -> str:
        args = ["snapshot"]
        if interactive_only:
            args.append("-i")
        return self._run(args)

    def snapshot_json(self, interactive_only: bool = True) -> list | dict:
        args = ["snapshot", "--json"]
        if interactive_only:
            args.append("-i")
        out = self._run(args)
        return json.loads(out) if out.strip() else []

    def click(self, ref: str, *, new_tab: bool = False) -> None:
        args = ["click", ref]
        if new_tab:
            args.append("--new-tab")
        self._run(args)

    def fill(self, ref: str, value: str) -> None:
        """Clear + type. For secrets, pass value here; never log it."""
        # agent-browser accepts value as positional arg, which could end up
        # in process listings on some systems. For this threat model
        # (local Windows, single user) it's acceptable; harden later via
        # auth vault if needed.
        self._run(["fill", ref, value])

    def type_text(self, ref: str, value: str) -> None:
        self._run(["type", ref, value])

    def press(self, key: str) -> None:
        self._run(["press", key])

    def focus(self, ref: str) -> None:
        self._run(["focus", ref])

    def find_click(self, locator_type: str, query: str, exact: bool = False) -> None:
        args = ["find", locator_type, query, "click"]
        if exact:
            args.append("--exact")
        self._run(args)

    def find_fill(self, locator_type: str, query: str, value: str) -> None:
        self._run(["find", locator_type, query, "fill", value])

    # -------------------------------------------------------------- waits

    def wait_url(self, pattern: str, timeout_s: int = 25) -> None:
        self._run(["wait", "--url", pattern, "--timeout", str(timeout_s * 1000)])

    def wait_text(self, text: str, timeout_s: int = 25) -> None:
        self._run(["wait", "--text", text, "--timeout", str(timeout_s * 1000)])

    def wait_load(self, event: str = "networkidle") -> None:
        self._run(["wait", "--load", event])

    def wait_ms(self, ms: int) -> None:
        self._run(["wait", str(ms)])

    # ------------------------------------------------------------ scripts

    def eval_js(self, js: str) -> str:
        """Run JS via stdin to avoid quoting problems. Returns stdout."""
        return self._run(["eval", "--stdin"], input_text=js)

    def eval_json(self, js: str):
        """Run JS and JSON-parse the result. Your JS must return JSON-serializable."""
        raw = self.eval_js(js).strip()
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return raw
        if isinstance(parsed, str):
            try:
                return json.loads(parsed)
            except json.JSONDecodeError:
                return parsed
        return parsed

    def get_text(self, ref: str) -> str:
        return self._run(["get", "text", ref])

    def get_attr(self, ref: str, attr: str) -> str:
        return self._run(["get", "attr", ref, attr]).strip()

    def get_html(self, ref: str) -> str:
        return self._run(["get", "html", ref])

    def clear_network_requests(self) -> None:
        self._run(["network", "requests", "--clear"])

    def network_post_count(self) -> int:
        requests = self._run(["network", "requests"])
        return sum(bool(re.search(r"\bPOST\b", line)) for line in requests.splitlines())

    def network_requests(self) -> str:
        """Raw `network requests` listing: `[id] METHOD URL (type) STATUS?` lines."""
        return self._run(["network", "requests"])

    def network_request_detail(self, request_id: str) -> str:
        """Full request detail incl. response body (`network request <id>`)."""
        return self._run(["network", "request", request_id])

    def screenshot(self, path: Path | str, full: bool = False) -> None:
        args = ["screenshot"]
        if full:
            args.append("--full")
        args.append(str(path))
        self._run(args)

    # ------------------------------------------------------------ session

    def save_state(self, path: Path | str) -> None:
        self._run(["state", "save", str(path)])

    def load_state(self, path: Path | str) -> None:
        self._run(["state", "load", str(path)])

    # ------------------------------------------------------------- retry

    def retry(self, func, tries: int = 3, delay: float = 1.5):
        last: Exception | None = None
        for i in range(tries):
            try:
                return func()
            except BrowserError as e:
                last = e
                log.warning("attempt %d/%d failed: %s", i + 1, tries, e)
                time.sleep(delay * (i + 1))
        raise last  # type: ignore[misc]
