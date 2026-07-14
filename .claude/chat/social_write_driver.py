"""Concrete agent-browser SocialWriteDriver (chat slice).

Implements the orchestration-layer `SocialWriteDriver` Protocol against the
visible-Chrome agent-browser helpers in `browser_control` and the append-only
redacted audit log in `browser_audit`. Lives in the chat slice so the
orchestration `BrowserExecutor` stays free of agent-browser imports — the
handler injects an instance of this class.

Hard invariants this driver upholds:
  - Visible-Chrome only (CDP 9222). `readiness` returns the physical
    `browser_readiness` envelope; the executor refuses when `enabled` is False.
    There is no launch/headless/fresh-profile path anywhere in `browser_control`.
  - Screenshots are PII-bearing (LinkedIn DOM/names/post body). `screenshot`
    persists the BYTES from `capture_browser_screenshot_png` to
    `DATA_DIR/browser_writes/<ts>-<workflow>.png` (git-ignored + sanitizer
    DENY_DIR `.claude/data/`) and returns the local PATH only — never the bytes,
    never a URL, never page text.
  - The executor never calls `gate(...)`. The `gate(...)` method here exists for
    the HANDLER's own use (it gates on the operator's verbatim message text,
    never on `payload_text`).
"""

from __future__ import annotations

import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from browser_audit import append_browser_audit_record
from browser_control import (
    browser_readiness,
    capture_browser_screenshot_png,
    redact_text_urls,
    resolve_cdp_port,
    run_agent_browser,
)
from browser_workflows import BrowserWorkflowDecision, require_browser_workflow_permission

# CDP env-name chain — LinkedIn and Primo X share the operator's persistent
# visible social browser profile on this machine.
_SOCIAL_CDP_ENV_NAMES = (
    "HOMIE_SOCIAL_CDP_PORT",
    "HOMIE_X_CDP_PORT",
    "X_BROWSER_CDP_PORT",
    "HOMIE_LINKEDIN_CDP_PORT",
    "LINKEDIN_BROWSER_CDP_PORT",
    "HOMIE_BROWSER_CDP_PORT",
    "AGENT_BROWSER_CDP_PORT",
)
_X_BROWSER_SESSION = "primo-x"


def _data_dir() -> Path:
    """Resolve DATA_DIR at call time (Rule 1 — no config value in a default arg)."""

    try:
        from config import DATA_DIR

        return Path(DATA_DIR)
    except Exception:  # pragma: no cover - import path fallback for direct scripts
        from personas import get_default_paths

        return get_default_paths()["data"]


def _memory_dir() -> Path:
    try:
        from config import MEMORY_DIR

        return Path(MEMORY_DIR)
    except Exception:  # pragma: no cover - import path fallback
        return Path(__file__).resolve().parents[2] / "TheHomie" / "Memory"


def _safe_workflow_slug(workflow_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", workflow_id).strip("-") or "social-write"


def _step_fail(label: str, result: Any) -> tuple[bool, str]:
    """Build a redacted (ok=False, detail) tuple for a failed agent-browser step."""

    detail = redact_text_urls(result.output[:600]) or "(no output)"
    return False, f"{label}: {detail}"


def _dismiss_windows_chrome_file_dialog() -> None:
    """Close only a Chrome-owned native ``Open`` dialog after CDP upload.

    Some visible-Chrome builds leave the Windows file picker onscreen even
    after ``agent-browser upload @ref <path>`` has successfully populated the
    input. The upload itself is still agent-browser/CDP-owned; this narrow
    cleanup prevents the stale native picker from covering the operator's
    desktop. Non-Windows hosts and non-Chrome dialogs are untouched.
    """

    import os

    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        import psutil

        user32 = ctypes.windll.user32
        callback_type = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
        )

        @callback_type
        def _visit(hwnd: int, _lparam: int) -> bool:
            title_len = user32.GetWindowTextLengthW(hwnd)
            if title_len <= 0:
                return True
            title = ctypes.create_unicode_buffer(title_len + 1)
            class_name = ctypes.create_unicode_buffer(128)
            user32.GetWindowTextW(hwnd, title, title_len + 1)
            user32.GetClassNameW(hwnd, class_name, len(class_name))
            if class_name.value != "#32770" or title.value not in {
                "Open",
                "Choose File",
            }:
                return True
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            try:
                process_name = psutil.Process(pid.value).name().lower()
            except Exception:
                return True
            if process_name in {"chrome.exe", "chrome"}:
                user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
            return True

        user32.EnumWindows(_visit, 0)
    except Exception:
        # Cleanup is best-effort; never turn a successful media upload into a
        # failed social write because the OS window inventory changed.
        return


class AgentBrowserSocialWriteDriver:
    """Visible-Chrome agent-browser implementation of SocialWriteDriver."""

    def __init__(self, *, screenshot_dir: Path | None = None) -> None:
        # Rule 1: resolve the screenshot dir at call time, not in a default arg.
        self._screenshot_dir = screenshot_dir

    # ── Approval gate (HANDLER's use only — executor never calls this) ──────
    def gate(
        self,
        workflow_id: str,
        operator_text: str,
        *,
        target_url: str | None = None,
    ) -> BrowserWorkflowDecision:
        """Default-deny gate on the operator's VERBATIM message text.

        NEVER pass `payload_text` (the post/comment body) here — only the
        operator's own message is approval text.
        """

        return require_browser_workflow_permission(
            workflow_id, operator_text, target_url=target_url
        )

    # ── Driver Protocol ─────────────────────────────────────────────────────
    def resolve_port(self) -> int:
        return resolve_cdp_port(env_names=_SOCIAL_CDP_ENV_NAMES)

    def readiness(self, *, port: int) -> dict:
        return browser_readiness(port=port)

    def drive(self, task: Any, *, port: int) -> tuple[bool, str]:
        """Drive the visible browser to land one social write.

        SELECTORS: verified against the live LinkedIn UI during the supervised
        first run (mirrors the reddit drive docstrings). The composer is a
        contenteditable role=textbox; the submit control is the "Post" button.
        For connect, the flow is Connect -> Add a note -> note textbox -> Send.
        """

        action = getattr(task, "action", "post")
        workflow_id = getattr(task, "workflow_id", "") or ""
        if action == "post":
            if workflow_id == "linkedin.post.create":
                return self._drive_post(task, port=port)
            if workflow_id == "x.post.create":
                return self._drive_x_post(task, port=port)
            return False, f"unsupported social post workflow: {workflow_id or '(missing)'}"
        if action == "connect":
            return self._drive_connect(task, port=port)
        return False, f"unsupported social-write action: {action}"

    def _drive_x_post(self, task: Any, *, port: int) -> tuple[bool, str]:
        """Publish one operator-approved post as the logged-in Primo X account."""

        def x_run(args: list[str], *, timeout: int) -> Any:
            return run_agent_browser(
                args,
                port=port,
                session=_X_BROWSER_SESSION,
                timeout=timeout,
            )

        body = getattr(task, "payload_text", "") or ""
        if not body.strip():
            return False, "X post body is empty"
        if len(body) > 280:
            return False, f"X post exceeds 280 characters ({len(body)})"

        compose_url = getattr(task, "target_url", "") or "https://x.com/compose/post"
        fresh_tab = None
        timed_out = False
        for _attempt in range(2):
            try:
                candidate = x_run(["tab", "new", compose_url], timeout=20)
            except subprocess.TimeoutExpired:
                # The isolated-session runner tree-kills the stale helper. One
                # retry now starts a clean primo-x helper against the same CDP.
                timed_out = True
                continue
            fresh_tab = candidate
            if candidate.ok:
                break
        if fresh_tab is None:
            return False, "X compose tab timed out twice"
        if not fresh_tab.ok:
            suffix = " after timeout retry" if timed_out else ""
            ok, detail = _step_fail("X compose tab failed", fresh_tab)
            return ok, f"{detail}{suffix}"
        for step in (["wait", "--load", "domcontentloaded"], ["wait", "2000"]):
            result = x_run(step, timeout=30)
            if not result.ok:
                return _step_fail(f"X {step[0]} failed", result)

        # X can leave the compose route on its loading spinner for longer than
        # the first 30-second accessibility snapshot window. Treat either a
        # timeout or an empty-but-successful tree as a hydration miss, wait for
        # the visible page to settle, and retry once before failing the row.
        snap = None
        editor_match = None
        for attempt in range(2):
            try:
                candidate = x_run(
                    ["snapshot", "-i"], timeout=30 if attempt == 0 else 45
                )
            except subprocess.TimeoutExpired:
                candidate = None
            snap = candidate
            if candidate is not None and candidate.ok:
                editor_match = re.search(
                    r'textbox "Post text" \[ref=(e\d+)\]',
                    candidate.stdout or "",
                )
                if editor_match:
                    break
            if attempt == 0:
                try:
                    x_run(["wait", "10000"], timeout=15)
                except subprocess.TimeoutExpired:
                    pass
        if snap is None:
            return False, "X composer snapshot timed out twice"
        if not snap.ok:
            return _step_fail("X composer snapshot failed", snap)
        if not editor_match:
            return False, "X composer textbox not found"
        editor_ref = editor_match.group(1)
        focus = x_run(["click", editor_ref], timeout=20)
        if not focus.ok:
            return _step_fail("X composer focus failed", focus)

        lines = body.split("\n")
        for idx, line in enumerate(lines):
            if line:
                typed = x_run(["keyboard", "inserttext", line], timeout=20)
                if not typed.ok:
                    return _step_fail("X post typing failed", typed)
            if idx < len(lines) - 1:
                enter = x_run(["press", "Enter"], timeout=15)
                if not enter.ok:
                    return _step_fail("X paragraph break failed", enter)

        readback = x_run(["get", "text", editor_ref], timeout=20)
        rb_len = len((readback.stdout or "").strip())
        if not readback.ok or rb_len < len(body) * 0.8:
            return False, f"X editor text incomplete after typing ({rb_len}/{len(body)} chars)"

        media_path = (getattr(task, "media_path", None) or "").strip()
        if media_path:
            media_file = Path(media_path).expanduser()
            if not media_file.is_file() or media_file.suffix.lower() not in {
                ".gif",
                ".jpeg",
                ".jpg",
                ".png",
                ".webp",
                ".mp4",
                ".mov",
            }:
                return False, "approved X media file is missing, unreadable, or unsupported"
            media_snap = x_run(["snapshot", "-i"], timeout=30)
            upload_match = re.search(
                r'button "Choose Files" \[ref=(e\d+)\]',
                media_snap.stdout or "",
            )
            if not upload_match:
                return False, "X media upload control not found"
            upload = x_run(
                ["upload", upload_match.group(1), str(media_file.resolve())],
                timeout=45,
            )
            if not upload.ok:
                return _step_fail("X media upload failed", upload)
            _dismiss_windows_chrome_file_dialog()
            x_run(["wait", "2500"], timeout=10)

        ready_snap = x_run(["snapshot", "-i"], timeout=30)
        if not ready_snap.ok:
            return _step_fail("X ready-state snapshot failed", ready_snap)
        post_match = re.search(
            r'button "Post" \[ref=(e\d+)\]', ready_snap.stdout or ""
        )
        if not post_match:
            return False, "X Post button did not become enabled"
        submit = x_run(["click", post_match.group(1)], timeout=30)
        if not submit.ok:
            return _step_fail("X post submit failed", submit)

        x_run(["wait", "2500"], timeout=10)
        verify = x_run(
            [
                "eval",
                "(()=>{const t=document.body.innerText||'';"
                "return /Your post was sent|View post/i.test(t)||location.pathname!='/compose/post'"
                "?'POSTED':'UNCONFIRMED';})()",
            ],
            timeout=15,
        )
        if verify.ok and "POSTED" in (verify.output or ""):
            return True, "X post submitted and confirmed"
        return False, "X submit returned without a confirmation"

    def _drive_post(self, task: Any, *, port: int) -> tuple[bool, str]:
        """Publish a feed post via the shadow-DOM composer (playbook §4.6).

        The composer editor is a Quill ``.ql-editor`` rendered inside a SHADOW
        ROOT — ``find role textbox`` / ``fill`` miss it and a plain
        ``querySelector`` returns nothing. The modal also opens as an empty
        shell and hydrates the editor a few seconds later. So: wait for
        hydration, shadow-pierce to the editor, synthetic-PASTE the body
        (base64'd so subprocess/shell quoting can't corrupt apostrophes,
        quotes, ``$``, ``#`` or newlines), then deep-find + ``.click()`` the
        enabled "Post" button (a CDP click is eaten). Confirm via the toast.
        """

        body = getattr(task, "payload_text", "") or ""
        if not body.strip():
            return False, "post body is empty"
        feed_url = getattr(task, "target_url", "") or "https://www.linkedin.com/feed/"

        # A REUSED tab can carry an injected overlay (e.g. the Gemini side
        # panel) that silently blocks the composer from opening. Always post
        # from a FRESH tab — proven the only reliable way to get the modal up.
        # `agent-browser tab new` without a URL can hang indefinitely against
        # the dedicated LinkedIn CDP session. Open the feed as part of tab
        # creation and check that result before continuing; otherwise the
        # executor records an opaque subprocess timeout after approval.
        fresh_tab = run_agent_browser(["tab", "new", feed_url], port=port, timeout=20)
        if not fresh_tab.ok:
            return _step_fail("fresh tab failed", fresh_tab)
        for step in (["wait", "--load", "networkidle"], ["wait", "3000"]):
            result = run_agent_browser(step, port=port, timeout=45)
            if not result.ok:
                return _step_fail(f"{step[0]} failed", result)

        # Open the composer with retries. The trigger may not be rendered yet,
        # and the modal opens as an empty shell whose editor hydrates a few
        # seconds later — poll the shadow DOM for the Quill editor rather than
        # trust the click result (a "Done" click can still leave it closed).
        editor_probe = (
            "(()=>{function deep(r){let a=[...r.querySelectorAll('*')];"
            "r.querySelectorAll('*').forEach(e=>{if(e.shadowRoot)a=a.concat(deep(e.shadowRoot));});return a;}"
            "return deep(document).find(e=>e.classList&&e.classList.contains('ql-editor')"
            "&&e.getAttribute('role')==='textbox')?'ED_OK':'NO_EDITOR';})()"
        )
        opened = False
        for _ in range(5):
            # Open via snapshot REF + `click @ref` (a CDP click) — proven
            # reliable. `find role button click --name` is flaky (it reports
            # done without opening). The utf-8 decode fix lets us parse the
            # snapshot safely; refs reach across the composer's frame boundary.
            snap = run_agent_browser(["snapshot", "-i"], port=port, timeout=30)
            # LinkedIn has exposed this trigger as both a button and a link.
            # Match either role; the accessible name + REF is the stable part.
            match = re.search(
                r'(?:button|link) "Start a post" \[ref=(e\d+)\]',
                snap.stdout or "",
            )
            if match:
                run_agent_browser(["click", match.group(1)], port=port, timeout=20)
                for _ in range(5):  # poll ~10s for the editor to hydrate
                    run_agent_browser(["wait", "2000"], port=port, timeout=8)
                    probe = run_agent_browser(["eval", editor_probe], port=port, timeout=20)
                    if probe.ok and "ED_OK" in (probe.output or ""):
                        opened = True
                        break
            if opened:
                break
            run_agent_browser(["wait", "2000"], port=port, timeout=8)  # feed still rendering
        if not opened:
            return False, "could not open the LinkedIn composer (trigger or editor not found)"

        # Focus the editor by its REF (a CDP click reaches across the
        # composer's frame boundary), then type the body LINE BY LINE.
        # `keyboard inserttext` truncates at newlines through the shell, and the
        # synthetic ClipboardEvent paste is ignored by Quill (untrusted) — so
        # real Enter key-presses make the paragraph breaks.
        snap = run_agent_browser(["snapshot", "-i"], port=port, timeout=30)
        editor_match = re.search(
            r'textbox "Text editor for creating content" \[ref=(e\d+)\]', snap.stdout or ""
        )
        if not editor_match:
            return False, "composer editor ref not found after open"
        editor_ref = editor_match.group(1)
        run_agent_browser(["click", editor_ref], port=port, timeout=20)
        lines = body.split("\n")
        for idx, line in enumerate(lines):
            if line:
                run_agent_browser(["keyboard", "inserttext", line], port=port, timeout=20)
            if idx < len(lines) - 1:
                run_agent_browser(["press", "Enter"], port=port, timeout=15)
        readback = run_agent_browser(["get", "text", editor_ref], port=port, timeout=20)
        rb_len = len((readback.stdout or "").strip())
        if rb_len < len(body) * 0.8:
            return False, f"editor text incomplete after typing ({rb_len}/{len(body)} chars)"

        media_path = (getattr(task, "media_path", None) or "").strip()
        if media_path:
            media_file = Path(media_path).expanduser()
            allowed_suffixes = {
                ".gif",
                ".jpeg",
                ".jpg",
                ".png",
                ".webp",
                ".mp4",
                ".avi",
                ".webm",
                ".wmv",
                ".flv",
                ".mpeg",
                ".mov",
                ".m4v",
            }
            if not media_file.is_file() or media_file.suffix.lower() not in allowed_suffixes:
                return False, "approved media file is missing, unreadable, or unsupported"

            # Open LinkedIn's media editor, target its accessible upload REF
            # (the hidden input lives behind the same shadow boundary), wait
            # for the preview, and return to the composer with Next.
            media_snap = run_agent_browser(["snapshot", "-i"], port=port, timeout=30)
            add_match = re.search(
                r'button "Add media" \[ref=(e\d+)\]', media_snap.stdout or ""
            )
            if not add_match:
                return False, "LinkedIn Add media control not found"
            add_result = run_agent_browser(
                ["click", add_match.group(1)], port=port, timeout=20
            )
            if not add_result.ok:
                return _step_fail("open media editor failed", add_result)
            run_agent_browser(["wait", "1000"], port=port, timeout=8)

            upload_snap = run_agent_browser(["snapshot", "-i"], port=port, timeout=30)
            upload_match = re.search(
                r'button "Upload from computer" \[ref=(e\d+)\]',
                upload_snap.stdout or "",
            )
            if not upload_match:
                return False, "LinkedIn media upload control not found"
            upload_result = run_agent_browser(
                ["upload", upload_match.group(1), str(media_file.resolve())],
                port=port,
                timeout=45,
            )
            if not upload_result.ok:
                return _step_fail("media upload failed", upload_result)
            _dismiss_windows_chrome_file_dialog()
            run_agent_browser(["wait", "2500"], port=port, timeout=10)

            preview_snap = run_agent_browser(["snapshot", "-i"], port=port, timeout=30)
            next_match = re.search(
                r'button "Next"(?: \[[^\]]*\])* \[ref=(e\d+)\]',
                preview_snap.stdout or "",
            ) or re.search(r'button "Next" \[ref=(e\d+)\]', preview_snap.stdout or "")
            if not next_match:
                return False, "LinkedIn media preview did not become ready"
            next_result = run_agent_browser(
                ["click", next_match.group(1)], port=port, timeout=20
            )
            if not next_result.ok:
                return _step_fail("media Next failed", next_result)
            run_agent_browser(["wait", "1500"], port=port, timeout=8)

            attached_snap = run_agent_browser(["snapshot", "-i"], port=port, timeout=30)
            if not re.search(
                r'button "(?:Edit media preview|Remove media)"',
                attached_snap.stdout or "",
            ):
                return False, "LinkedIn media attachment was not confirmed"

        # Give LinkedIn a beat to enable the Post button after the input lands.
        run_agent_browser(["wait", "2000"], port=port, timeout=8)

        # CDP click on "Post" is eaten by overlays — deep-find the enabled
        # BUTTON whose text is exactly "Post" and fire its real onClick.
        click_js = (
            "(()=>{function deep(r){let a=[...r.querySelectorAll('*')];"
            "r.querySelectorAll('*').forEach(e=>{if(e.shadowRoot)a=a.concat(deep(e.shadowRoot));});return a;}"
            "const b=deep(document).find(e=>e.tagName==='BUTTON'&&!e.disabled"
            "&&(e.innerText||'').trim()==='Post');"
            "if(!b)return 'NO_POST_BTN';b.click();return 'CLICKED';})()"
        )
        submit = run_agent_browser(["eval", click_js], port=port, timeout=40)
        if not submit.ok:
            return _step_fail("post submit failed", submit)
        if "CLICKED" not in (submit.output or ""):
            return False, f"post button not found: {redact_text_urls((submit.output or '')[:200])}"

        # Confirm via the "Post successful / View post" toast (Rule 7).
        run_agent_browser(["wait", "3000"], port=port, timeout=10)
        verify = run_agent_browser(
            ["eval", "(()=>/Post successful|View post/i.test(document.body.innerText||'')?'POSTED':'UNCONFIRMED')()"],
            port=port,
            timeout=15,
        )
        if verify.ok and "POSTED" in (verify.output or ""):
            return True, "post submitted and confirmed"
        return True, "post submitted (confirmation toast not detected — verify manually)"

    def _drive_connect(self, task: Any, *, port: int) -> tuple[bool, str]:
        note = getattr(task, "payload_text", "") or ""
        profile_url = getattr(task, "target_url", "") or ""
        if not profile_url:
            return False, "connect requires a target profile URL"
        for step in (["open", profile_url], ["wait", "--load", "networkidle"]):
            result = run_agent_browser(step, port=port)
            if not result.ok:
                return _step_fail(f"{step[0]} failed", result)
        connect = run_agent_browser(
            ["find", "role", "button", "click", "--name", "Connect"], port=port
        )
        if not connect.ok:
            return _step_fail("connect click failed", connect)
        if note:
            add_note = run_agent_browser(
                ["find", "role", "button", "click", "--name", "Add a note"], port=port
            )
            if not add_note.ok:
                return _step_fail("add-a-note failed", add_note)
            fill = run_agent_browser(["find", "role", "textbox", "fill", note], port=port)
            if not fill.ok:
                return _step_fail("note fill failed", fill)
        send = run_agent_browser(["find", "role", "button", "click", "--name", "Send"], port=port)
        if not send.ok:
            return _step_fail("send invite failed", send)
        return True, "connection request sent"

    def screenshot(self, *, port: int, workflow_id: str) -> str | None:
        """Persist the screenshot BYTES to a git-ignored file and return the PATH.

        `capture_browser_screenshot_png` returns bytes and deletes its temp file,
        so the driver owns persistence. The PNG is PII-bearing — only the local
        path enters the receipt metadata, never the bytes.
        """

        data = capture_browser_screenshot_png(port=port)
        out_dir = self._screenshot_dir or (_data_dir() / "browser_writes")
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        out_path = out_dir / f"{ts}-{_safe_workflow_slug(workflow_id)}.png"
        out_path.write_bytes(data)
        return str(out_path)

    def audit(self, **kwargs: Any) -> None:
        append_browser_audit_record(**kwargs)


def make_social_write_driver(
    *, screenshot_dir: Path | None = None
) -> AgentBrowserSocialWriteDriver:
    """Factory for the visible-Chrome social-write driver.

    Consumed by the unified social ``post_executor`` browser-dispatch path;
    mirrors the in-handler ``AgentBrowserSocialWriteDriver()`` construction the
    proven ``/linkedin_post`` path uses. Rule-1 safe: ``screenshot_dir`` is a
    None sentinel resolved inside the driver at call time.
    """
    return AgentBrowserSocialWriteDriver(screenshot_dir=screenshot_dir)


# ── Tracker-append helper (HANDLER calls this on success) ──────────────────


def append_tracker_row(
    *,
    name: str,
    lane: str,
    action: str,
    status: str,
    notes: str = "",
    tracker_path: Path | None = None,
) -> bool:
    """Append one row under the `## Touched` section of the outreach tracker.

    Markdown-only, no new state surface. Returns True on a successful append,
    False (fail-open) if the tracker is missing or the section is absent — a
    tracker write must never fail a landed social write.
    """

    path = tracker_path or (_memory_dir() / "docs" / "LINKEDIN-OUTREACH-TRACKER.md")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    marker = "## Touched"
    idx = text.find(marker)
    if idx == -1:
        return False
    # Find the header row + separator, then locate the end of the existing table
    # so the new row appends to the bottom of the table (before the next "##").
    next_section = text.find("\n## ", idx + len(marker))
    end = next_section if next_section != -1 else len(text)
    head = text[:end].rstrip("\n")
    tail = text[end:]
    date = datetime.now(UTC).strftime("%Y-%m-%d")

    def _cell(value: str) -> str:
        return redact_text_urls(str(value)).replace("|", "\\|").strip()

    row = (
        f"| {date} | {_cell(name)} | {_cell(lane)} | {_cell(action)} "
        f"| {_cell(status)} | {_cell(notes)} |"
    )
    new_text = f"{head}\n{row}\n{tail}" if tail else f"{head}\n{row}\n"
    try:
        path.write_text(new_text, encoding="utf-8")
    except OSError:
        return False
    return True
