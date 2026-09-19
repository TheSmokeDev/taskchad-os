"""Concrete agent-browser SocialWriteDriver (chat slice).

Implements the orchestration-layer `SocialWriteDriver` Protocol against the
visible-Chrome agent-browser helpers in `browser_control` and the append-only
redacted audit log in `browser_audit`. Lives in the chat slice so the
orchestration `BrowserExecutor` stays free of agent-browser imports — the
handler injects an instance of this class.

Hard invariants this driver upholds:
  - Visible-Chrome only (CDP 18222). `readiness` returns the physical
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

import hashlib
import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

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
_LINKEDIN_BROWSER_SESSION = "linkedin-social"
_LINKEDIN_PERMALINK_RE = re.compile(
    r"(?:https://(?:www\.)?linkedin\.com)?"
    r"(/feed/update/urn:li:(?:share|activity):\d+/?)",
    re.IGNORECASE,
)


def _browser_json(result: Any) -> dict[str, Any]:
    """Decode an Agent Browser read-only JSON projection, never raw page state."""

    if not result.ok:
        return {}
    try:
        value = json.loads(result.stdout or "")
        if isinstance(value, str):
            value = json.loads(value)
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def _company_composer_url(publisher: Any) -> str:
    return (
        f"https://www.linkedin.com/company/{publisher.id}"
        "/admin/page-posts/published/?share=true"
    )


def _company_composer_probe() -> str:
    """Read visible composer actor attributes; do not infer actor from page name.

    The current company composer has no numeric actor attribute. Bind its
    exact name and logo to the selected numeric organization admin navigation,
    in addition to the exact admin URL. A name or background link alone is not
    evidence. The probe only reads visible DOM; interaction uses current refs.
    """

    return """(() => {
      function deep(r) { const a=[...r.querySelectorAll('*')];
        return a.flatMap(e=>[e,...(e.shadowRoot?deep(e.shadowRoot):[])]); }
      const nodes=deep(document);
      const visible=e=>!!(e.getClientRects().length);
      const dialogs=nodes.filter(e=>visible(e)&&e.matches('[role="dialog"].share-box-v2__modal'));
      if(dialogs.length!==1) return {actor_count:0};
      const actors=deep(dialogs[0]).filter(e=>visible(e) &&
        (e.tagName==='BUTTON'||e.getAttribute('role')==='button') &&
        /Post to (Anyone|Connections)/i.test(
          (e.innerText||'')+' '+(e.getAttribute('aria-label')||'')));
      if(actors.length!==1) return {actor_count:actors.length};
      const actor=actors[0], scoped=deep(actor);
      const navSelector='section[aria-label="Organizational page admin navigation section"]';
      const navs=nodes.filter(e=>visible(e)&&e.matches(navSelector));
      if(navs.length!==1) return {actor_count:1};
      const nav=deep(navs[0]);
      const current='a.org-menu-item--selected[aria-current="true"][href], '+
        'a.org-menu-item--selected[aria-current="page"][href]';
      const selected=nav.filter(e=>visible(e)&&e.matches(current));
      // A compact admin layout removes the vertical Page posts item. Its
      // observed Published tab is in the bounded org feed header menu instead.
      const header='section.org-feed__page-header .org-page-header__menu ';
      selected.push(...nodes.filter(e=>visible(e)&&e.matches(
        header+'a.org-menu-item--selected[aria-current="true"][href], '+
        header+'a.org-menu-item--selected[aria-current="page"][href]')));
      const selected_urls=[...new Set(selected.map(e=>e.href))];
      const heads=nav.filter(e=>visible(e)&&e.tagName==='H1');
      const logos=nav.filter(e=>visible(e)&&e.tagName==='IMG');
      const actorImages=scoped.filter(e=>visible(e)&&e.tagName==='IMG');
      const imageKey=e=>{try {const u=new URL(e.currentSrc||e.src);return u.origin+u.pathname;}
        catch(_){return '';}};
      return {actor_count:1, actor_text:actor.innerText||actor.getAttribute('aria-label')||'',
        actor_names:actorImages.map(e=>e.alt), actor_logos:actorImages.map(imageKey),
        admin_names:heads.map(e=>e.innerText), admin_logos:logos.map(imageKey),
        selected_urls, url:location.href};
    })()"""


def _valid_company_composer_proof(proof: dict, publisher: Any) -> bool:
    parsed = urlsplit(str(proof.get("url") or ""))
    expected_path = f"/company/{publisher.id}/admin/page-posts/published/"
    selected = proof.get("selected_urls")
    if not isinstance(selected, list) or len(selected) != 1:
        return False
    selected_url = urlsplit(str(selected[0]))
    expected_actor = " ".join(publisher.name.split()).casefold()
    actor_text = " ".join(str(proof.get("actor_text") or "").split()).casefold()
    # innerText may omit duplicated avatar-alt text, unlike the AX snapshot.
    valid_actor_text = {
        f"{name} post to {audience}"
        for name in (expected_actor, f"{expected_actor} {expected_actor}")
        for audience in ("anyone", "connections")
    }
    actor_logos = proof.get("actor_logos")
    admin_logos = proof.get("admin_logos")
    expected_name = " ".join(publisher.name.split())
    actor_names = proof.get("actor_names")
    admin_names = proof.get("admin_names")
    names_bound = (
        isinstance(actor_names, list) and isinstance(admin_names, list)
        and [" ".join(str(name).split()) for name in actor_names] == [expected_name]
        and [" ".join(str(name).split()) for name in admin_names] == [expected_name]
    )
    logo_bound = (
        isinstance(actor_logos, list) and len(actor_logos) == 1
        and bool(actor_logos[0]) and isinstance(admin_logos, list)
        and actor_logos[0] in admin_logos
    )
    return (
        parsed.scheme == "https"
        and parsed.hostname in {"linkedin.com", "www.linkedin.com"}
        and parsed.path.rstrip("/") == expected_path.rstrip("/")
        and selected_url.scheme == "https"
        and selected_url.hostname in {"linkedin.com", "www.linkedin.com"}
        and selected_url.path.rstrip("/") == expected_path.rstrip("/")
        and proof.get("actor_count") == 1
        and names_bound
        and actor_text in valid_actor_text
        and logo_bound
    )


def _company_post_probe(permalink: str) -> str:
    """Project a single permalink post's public author, caption, and media.

    Only the actual post container is inspected, not the page's navigation,
    comments, author avatar, or suggested posts. Unknown DOM layouts fail closed.
    """

    urn = urlsplit(permalink).path.rstrip("/").rsplit("/", 1)[-1]
    return """(() => {
      const expectedUrn=URN;
      function deep(r) { const a=[...r.querySelectorAll('*')];
        return a.flatMap(e=>[e,...(e.shadowRoot?deep(e.shadowRoot):[])]); }
      const all=deep(document), visible=e=>!!e.getClientRects().length;
      let posts=all.filter(e=>visible(e)&&e.getAttribute('data-urn')===expectedUrn);
      if(!posts.length) posts=all.filter(e=>visible(e)&&e.getAttribute('role')==='article');
      posts=posts.filter(e=>!posts.some(p=>p!==e&&p.contains(e)));
      if(posts.length!==1) return {post_count:posts.length};
      const post=posts[0], nodes=deep(post);
      const textSelector='.update-components-text, [data-view-name="feed-commentary"]';
      const text=nodes.filter(e=>visible(e)&&e.matches(textSelector));
      const captions=text.filter(e=>!text.some(p=>p!==e&&p.contains(e)))
        .map(e=>(e.querySelector('.break-words')||e).innerText||'');
      const actorSelector='a.update-components-actor__meta-link, '+
        'a.update-components-actor__image, a[data-view-name="feed-actor-name"]';
      const actorLinks=nodes.filter(e=>visible(e)&&e.matches(actorSelector));
      const actor_urls=actorLinks.map(e=>e.href).filter(Boolean);
      const imageSelector='.update-components-actor img, img.update-components-actor__avatar-image';
      const actorImages=nodes.filter(e=>visible(e)&&e.matches(imageSelector));
      const actor_logos=actorImages.map(e=>{try{const u=new URL(e.currentSrc||e.src);
        return u.origin+u.pathname;}catch(_){return '';}});
      const mediaSelector='.update-components-image img, .update-components-video video, '+
        '[data-view-name="feed-image"] img';
      const media=nodes.filter(e=>visible(e)&&e.matches(mediaSelector));
      return {post_count:1, actor_urls, actor_logos, captions,
        media_count:media.length, url:location.href};
    })()""".replace("URN", json.dumps(urn))


def _valid_company_post_proof(
    proof: dict, publisher: Any, body: str, *, expected_logo: str | None = None,
) -> bool:
    if proof.get("post_count") != 1 or proof.get("media_count", 0) < 1:
        return False
    captions = proof.get("captions")
    if not isinstance(captions, list) or len(captions) != 1:
        return False
    if " ".join(str(captions[0]).split()) != " ".join(body.split()):
        return False
    urls = proof.get("actor_urls")
    if not isinstance(urls, list) or not urls:
        return False
    expected_paths = {
        urlsplit(publisher.url).path.rstrip("/"), f"/company/{publisher.id}",
    }
    for raw in urls:
        url = urlsplit(str(raw))
        if (
            url.scheme != "https"
            or url.hostname not in {"linkedin.com", "www.linkedin.com"}
            or url.path.rstrip("/") not in expected_paths
        ):
            return False
    if expected_logo and expected_logo not in (proof.get("actor_logos") or []):
        return False
    return True


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


def _unique_enabled_button_ref(snapshot: str, name: str) -> str | None:
    """Select one exact enabled button, deduplicating repeated AX output."""

    refs = set()
    for line in snapshot.splitlines():
        match = re.fullmatch(
            rf'\s*(?:-\s*)?button "{re.escape(name)}"(?P<attrs>.*)', line,
        )
        if not match or re.search(r"\bdisabled\b", match.group("attrs")):
            continue
        ref = re.search(r"\bref=(e\d+)\b", match.group("attrs"))
        if ref:
            refs.add(ref.group(1))
    return next(iter(refs)) if len(refs) == 1 else None


def _company_media_next_xpath() -> str:
    # v0.33.2 native/element.rs resolves xpath= directly to the DOM and uses a
    # real CDP click, independent of the mutable eN RefMap. The light-DOM modal,
    # button role and exact Next label were observed in the supervised canary.
    return (
        "xpath=//*[@role='dialog' and "
        "contains(concat(' ',normalize-space(@class),' '),' share-box-v2__modal ')]"
        "//button[normalize-space(.)='Next' and not(@disabled) "
        "and not(@aria-disabled='true')]"
    )


def _advance_company_media(li_run: Any, *, scope: str, port: int) -> tuple[bool, str]:
    """Advance only the exact company media Next control, without cached refs.

    Require fresh scoped accessibility evidence and one matching enabled DOM
    button. All failures and timeouts stop; neither this step, uploads, nor the
    public Post control are retried.
    """

    selector = _company_media_next_xpath()
    xpath = json.dumps(selector.removeprefix("xpath="))
    readiness = (
        "(()=>{const nodes=document.evaluate(" + xpath
        + ",document,null,XPathResult.ORDERED_NODE_SNAPSHOT_TYPE,null);"
        "return nodes.snapshotLength===1&&nodes.snapshotItem(0).getClientRects().length>0;})()"
    )
    ready = li_run(["wait", "--fn", readiness], port=port, timeout=25)
    if not ready.ok:
        return _step_fail("media preview readiness failed", ready)
    snapshot = li_run(["snapshot", "-i", "-s", scope], port=port, timeout=30)
    if not snapshot.ok:
        return _step_fail("media preview snapshot failed", snapshot)
    if not _unique_enabled_button_ref(snapshot.stdout or "", "Next"):
        return False, "LinkedIn media preview has no unique enabled Next control"
    count = li_run(["get", "count", selector], port=port, timeout=20)
    if not count.ok or (count.stdout or "").strip() != "1":
        return False, "LinkedIn media Next DOM identity is missing or ambiguous"
    result = li_run(["click", selector], port=port, timeout=20)
    return (True, "media Next advanced") if result.ok else _step_fail("media Next failed", result)


def _validated_linkedin_permalink(raw: str) -> str | None:
    """Extract one public LinkedIn post permalink from agent-browser output."""

    normalized = (raw or "").replace("\\/", "/")
    match = _LINKEDIN_PERMALINK_RE.search(normalized)
    if not match:
        return None
    candidate = match.group(0)
    if candidate.startswith("/"):
        candidate = urljoin("https://www.linkedin.com", candidate)
    parsed = urlsplit(candidate)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in {
        "linkedin.com",
        "www.linkedin.com",
    }:
        return None
    if not _LINKEDIN_PERMALINK_RE.fullmatch(parsed.path):
        return None
    return urlunsplit(("https", "www.linkedin.com", parsed.path, "", ""))


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
        self._last_verification: dict[str, str] = {}

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

        self._last_verification = {}
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

    def _drive_post(
        self, task: Any, *, port: int, prepare_only: bool = False,
    ) -> tuple[bool, str]:
        """Publish approved copy/media through a dedicated visible-CDP session."""

        bound_tab_id: str | None = None
        bound_paths: set[str] = set()
        refs_live = False

        def raw_run(args: list[str], *, port: int, timeout: int) -> Any:
            return run_agent_browser(
                args, port=port, session=_LINKEDIN_BROWSER_SESSION, timeout=timeout,
            )

        def li_run(args: list[str], *, port: int, timeout: int) -> Any:
            nonlocal refs_live
            if bound_tab_id:
                inventory = _browser_json(raw_run(["tab", "--json"], port=port, timeout=20))
                data = inventory.get("data")
                tabs = data.get("tabs") if isinstance(data, dict) else None
                if inventory.get("success") is not True or not isinstance(tabs, list):
                    raise ValueError("LinkedIn company tab inventory unavailable; drive stopped")
                active = [
                    tab.get("tabId") for tab in tabs
                    if isinstance(tab, dict) and tab.get("active") is True
                ]
                if active != [bound_tab_id]:
                    # Selecting even the SAME tab resets Agent Browser refs.
                    # Reselect only before a new snapshot (or initial hydration)
                    # and never between an existing snapshot and its actions.
                    if refs_live and args[0] != "snapshot":
                        raise ValueError(
                            "LinkedIn company tab changed after snapshot; drive stopped"
                        )
                    selected = raw_run(["tab", bound_tab_id], port=port, timeout=20)
                    if not selected.ok:
                        raise ValueError("LinkedIn company tab selection failed; drive stopped")
                    refs_live = False
                current = raw_run(["get", "url"], port=port, timeout=20)
                current_url = urlsplit((current.stdout or "").strip().strip('"'))
                if (
                    not current.ok
                    or current_url.scheme != "https"
                    or current_url.hostname not in {"linkedin.com", "www.linkedin.com"}
                    or current_url.path.rstrip("/") not in bound_paths
                ):
                    raise ValueError("LinkedIn company tab identity changed; drive stopped")
            result = raw_run(args, port=port, timeout=timeout)
            if args[0] == "snapshot" and result.ok:
                refs_live = True
            elif args[0] in {"open", "tab"}:
                refs_live = False
            return result

        body = getattr(task, "payload_text", "") or ""
        if not body.strip():
            return False, "post body is empty"
        from social.publishers import parse_publisher

        try:
            publisher = parse_publisher(getattr(task, "publisher_json", None))
        except ValueError:
            return False, "invalid approved LinkedIn publisher snapshot"
        feed_url = getattr(task, "target_url", "") or "https://www.linkedin.com/feed/"
        media_path = (getattr(task, "media_path", None) or "").strip()
        if publisher:
            if feed_url != _company_composer_url(publisher):
                return False, "company target URL differs from the approved publisher"
            if not media_path or not Path(media_path).is_file():
                return False, "company draft requires the reviewed image; nothing submitted"
        elif "/company/" in urlsplit(feed_url).path:
            return False, "company target requires an approved publisher snapshot"
        composer_scope = (
            '[role="dialog"].share-box-v2__modal' if publisher else "#interop-outlet"
        )

        # A REUSED tab can carry an injected overlay (e.g. the Gemini side
        # panel) that silently blocks the composer from opening. Always post
        # from a FRESH tab — proven the only reliable way to get the modal up.
        # `agent-browser tab new` without a URL can hang indefinitely against
        # the dedicated LinkedIn CDP session. Open the feed as part of tab
        # creation and check that result before continuing; otherwise the
        # executor records an opaque subprocess timeout after approval.
        before_ids: set[str] = set()
        if publisher:
            before = _browser_json(raw_run(["tab", "--json"], port=port, timeout=20))
            before_data = before.get("data")
            tabs = before_data.get("tabs") if isinstance(before_data, dict) else None
            if before.get("success") is not True or not isinstance(tabs, list):
                return False, "company browser tab inventory unavailable; nothing submitted"
            before_ids = {str(tab.get("tabId")) for tab in tabs if isinstance(tab, dict)}
        fresh_tab = li_run(["tab", "new", feed_url], port=port, timeout=45)
        if not fresh_tab.ok:
            return _step_fail("fresh tab failed", fresh_tab)
        if publisher:
            after = _browser_json(raw_run(["tab", "--json"], port=port, timeout=20))
            after_data = after.get("data")
            tabs = after_data.get("tabs", []) if isinstance(after_data, dict) else []
            added = [
                tab for tab in tabs if isinstance(tab, dict)
                and tab.get("tabId") not in before_ids
                and re.fullmatch(r"t\d+", str(tab.get("tabId") or ""))
                and urlsplit(str(tab.get("url") or "")).hostname
                in {"linkedin.com", "www.linkedin.com"}
                and urlsplit(str(tab.get("url") or "")).path.rstrip("/")
                == urlsplit(feed_url).path.rstrip("/")
            ]
            if after.get("success") is not True or len(added) != 1:
                return False, "fresh company browser tab identity is ambiguous; nothing submitted"
            bound_tab_id = str(added[0]["tabId"])
            bound_paths.add(urlsplit(feed_url).path.rstrip("/"))
            # Tab inventory can report the pending destination before the
            # document commits. Settle that one new tab BEFORE strict steady-
            # state guards or snapshot refs exist. Only about:blank is a valid
            # transient; login, wrong origins, and wrong companies fail closed.
            selected = raw_run(["tab", bound_tab_id], port=port, timeout=20)
            bootstrap = raw_run(["get", "url"], port=port, timeout=20)
            if not selected.ok or not bootstrap.ok:
                return False, "fresh company tab bootstrap failed; nothing submitted"
            if (bootstrap.stdout or "").strip().strip('"') == "about:blank":
                expected_url = feed_url.split("?", 1)[0] + "**"
                settled = raw_run(
                    ["wait", "--url", expected_url], port=port, timeout=45,
                )
                if not settled.ok:
                    return False, "company navigation did not settle; nothing submitted"
                bootstrap = raw_run(["get", "url"], port=port, timeout=20)
            bootstrap_url = urlsplit((bootstrap.stdout or "").strip().strip('"'))
            if (
                not bootstrap.ok or bootstrap_url.scheme != "https"
                or bootstrap_url.hostname not in {"linkedin.com", "www.linkedin.com"}
                or bootstrap_url.path.rstrip("/") not in bound_paths
            ):
                return False, "company navigation reached an unapproved URL; nothing submitted"
        for step in (["wait", "--load", "domcontentloaded"], ["wait", "3000"]):
            result = li_run(step, port=port, timeout=45)
            if not result.ok:
                return _step_fail(f"{step[0]} failed", result)

        # Open the composer with retries. The trigger may not be rendered yet,
        # and the modal opens as an empty shell whose editor hydrates a few
        # seconds later — poll the shadow DOM for the Quill editor rather than
        # trust the click result (a "Done" click can still leave it closed).
        editor_probe = (
            "(()=>{function deep(r){let a=[...r.querySelectorAll('*')];"
            "r.querySelectorAll('*').forEach(e=>{if(e.shadowRoot)"
            "a=a.concat(deep(e.shadowRoot));});return a;}"
            "return deep(document).find(e=>e.classList&&e.classList.contains('ql-editor')"
            "&&e.getAttribute('role')==='textbox')?'ED_OK':'NO_EDITOR';})()"
        )
        opened = False
        for _ in range(5):
            if publisher:
                # The allowlisted company admin URL bootstraps the existing
                # share composer. Never click a personal Start a post fallback.
                probe = li_run(["eval", editor_probe], port=port, timeout=20)
                if probe.ok and "ED_OK" in (probe.output or ""):
                    opened = True
                    break
                li_run(["wait", "2000"], port=port, timeout=8)
                continue
            # Open via snapshot REF + `click @ref` (a CDP click) — proven
            # reliable. `find role button click --name` is flaky (it reports
            # done without opening). The utf-8 decode fix lets us parse the
            # snapshot safely; refs reach across the composer's frame boundary.
            snap = li_run(["snapshot", "-i"], port=port, timeout=30)
            # LinkedIn has exposed this trigger as both a button and a link.
            # Match either role; the accessible name + REF is the stable part.
            match = re.search(
                r'(?:button|link) "Start a post" \[ref=(e\d+)\]',
                snap.stdout or "",
            )
            if match:
                li_run(["click", match.group(1)], port=port, timeout=20)
                for _ in range(5):  # poll ~10s for the editor to hydrate
                    li_run(["wait", "2000"], port=port, timeout=8)
                    probe = li_run(["eval", editor_probe], port=port, timeout=20)
                    if probe.ok and "ED_OK" in (probe.output or ""):
                        opened = True
                        break
            if opened:
                break
            li_run(["wait", "2000"], port=port, timeout=8)  # feed still rendering
        if not opened:
            return False, "could not open the LinkedIn composer (trigger or editor not found)"

        if publisher:
            actor = _browser_json(li_run(
                ["eval", _company_composer_probe()], port=port, timeout=20,
            ))
            if not _valid_company_composer_proof(actor, publisher):
                return False, "company composer publisher could not be verified; nothing uploaded"
            expected_company_logo = str(actor["actor_logos"][0])
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
            media_snap = li_run(
                ["snapshot", "-i", "-s", composer_scope], port=port, timeout=30,
            )
            add_match = re.search(
                r'button "Add media" \[ref=(e\d+)\]', media_snap.stdout or ""
            )
            if not add_match:
                return False, "LinkedIn Add media control not found"
            add_result = li_run(
                ["click", add_match.group(1)], port=port, timeout=20
            )
            if not add_result.ok:
                return _step_fail("open media editor failed", add_result)
            li_run(["wait", "1000"], port=port, timeout=8)

            upload_snap = li_run(

                ["snapshot", "-i", "-s", composer_scope], port=port, timeout=30,

            )
            upload_match = re.search(
                r'button "Upload from computer" \[ref=(e\d+)\]',
                upload_snap.stdout or "",
            )
            if not upload_match:
                return False, "LinkedIn media upload control not found"
            upload_result = li_run(
                ["upload", upload_match.group(1), str(media_file.resolve())],
                port=port,
                timeout=45,
            )
            if not upload_result.ok:
                return _step_fail("media upload failed", upload_result)
            _dismiss_windows_chrome_file_dialog()
            li_run(["wait", "2500"], port=port, timeout=10)

            if publisher:
                advanced, detail = _advance_company_media(
                    li_run, scope=composer_scope, port=port,
                )
                if not advanced:
                    return False, detail
            else:
                preview_snap = li_run(
                    ["snapshot", "-i", "-s", composer_scope], port=port, timeout=30,
                )
                next_match = re.search(
                    r'button "Next"(?: \[[^\]]*\])* \[ref=(e\d+)\]',
                    preview_snap.stdout or "",
                ) or re.search(r'button "Next" \[ref=(e\d+)\]', preview_snap.stdout or "")
                if not next_match:
                    return False, "LinkedIn media preview did not become ready"
                next_result = li_run(
                    ["click", next_match.group(1)], port=port, timeout=20,
                )
                if not next_result.ok:
                    return _step_fail("media Next failed", next_result)
            li_run(["wait", "1500"], port=port, timeout=8)

            attached_snap = li_run(

                ["snapshot", "-i", "-s", composer_scope], port=port, timeout=30,

            )
            if not re.search(
                r'button "(?:Edit media preview|Remove media)"',
                attached_snap.stdout or "",
            ):
                return False, "LinkedIn media attachment was not confirmed"

        # Media upload/Next can rebuild the composer and discard existing text.
        # Enter the approved caption only after the media editor has finished.
        caption_snapshot = li_run(
            ["snapshot", "-i", "-s", composer_scope], port=port, timeout=30,
        )
        editor_match = re.search(
            r'textbox "Text editor for creating content" \[ref=(e\d+)\]',
            caption_snapshot.stdout or "",
        )
        if not caption_snapshot.ok or not editor_match:
            return False, "composer editor ref not found after media upload"
        editor_ref = editor_match.group(1)
        focused = li_run(["click", editor_ref], port=port, timeout=20)
        if not focused.ok:
            return _step_fail("caption focus failed", focused)
        # Clear any existing LinkedIn draft text without changing the queue row.
        for command in (["press", "Control+a"], ["press", "Backspace"]):
            cleared = li_run(command, port=port, timeout=15)
            if not cleared.ok:
                return _step_fail("caption clear failed", cleared)
        lines = body.split("\n")
        for idx, line in enumerate(lines):
            if line:
                typed = li_run(["keyboard", "inserttext", line], port=port, timeout=20)
                if not typed.ok:
                    return _step_fail("caption input failed", typed)
            if idx < len(lines) - 1:
                entered = li_run(["press", "Enter"], port=port, timeout=15)
                if not entered.ok:
                    return _step_fail("caption paragraph failed", entered)

        # Give LinkedIn a beat to enable the Post button after the input lands.
        li_run(["wait", "2000"], port=port, timeout=8)

        # Resolve the enabled submit control from the current snapshot. Stamp
        # ambiguity BEFORE clicking: even a click timeout may have submitted.
        submit_snapshot = li_run(
            ["snapshot", "-i", "-s", composer_scope], port=port, timeout=30,
        )
        submit_match = re.search(
            r'button "Post" \[ref=(e\d+)\]', submit_snapshot.stdout or ""
        )
        if not submit_snapshot.ok or not submit_match:
            return False, "enabled LinkedIn Post control not found"
        # Read the fresh editor ref after all upload/navigation/input work.
        # Full text equality is required, never a length or substring threshold.
        final_editor = re.search(
            r'textbox "Text editor for creating content" \[ref=(e\d+)\]',
            submit_snapshot.stdout or "",
        )
        if not final_editor:
            return False, "final caption editor not found; nothing submitted"
        final_caption = li_run(
            ["get", "text", final_editor.group(1)], port=port, timeout=20,
        )
        expected_text = " ".join(body.split())
        observed_text = " ".join((final_caption.stdout or "").split())
        if not final_caption.ok or observed_text != expected_text:
            return False, "final caption does not match the approved copy; nothing submitted"
        if media_path and not re.search(
            r'button "(?:Edit media preview|Remove media)"', submit_snapshot.stdout or "",
        ):
            return False, "approved media missing from final composer; nothing submitted"
        caption_proof = {
            "caption_verified_before_submit": "true",
            "expected_caption_sha256": hashlib.sha256(expected_text.encode()).hexdigest(),
            "observed_caption_sha256": hashlib.sha256(observed_text.encode()).hexdigest(),
            "media_verified_before_submit": "true" if media_path else "not_requested",
        }
        if publisher:
            # Re-read after upload/Next and text entry: either can rebuild the
            # composer. An actor switch never inherits the earlier approval.
            actor = _browser_json(li_run(
                ["eval", _company_composer_probe()], port=port, timeout=20,
            ))
            if not _valid_company_composer_proof(actor, publisher):
                return False, "final company publisher does not match approval; nothing submitted"
            caption_proof.update({
                "publisher_id": publisher.id,
                "publisher_verified_before_submit": "true",
            })
        if prepare_only:
            self._last_verification = {
                **caption_proof,
                "verification_state": "prepared_only",
                "confirmation_result": "caption_and_media_ready_no_submit",
            }
            return True, "caption and media verified in composer; not submitted"

        submitted_at = datetime.now(UTC).isoformat(timespec="seconds")
        self._last_verification = {
            **caption_proof,
            "verification_state": "verification_required",
            "post_url": "",
            "submitted_at": submitted_at,
            "confirmation_result": "submit_attempted_proof_pending",
        }
        try:
            submit = li_run(["click", submit_match.group(1)], port=port, timeout=40)
        except Exception:
            return True, "post submit outcome unknown; permalink verification required"
        if not submit.ok:
            return True, "post submit outcome unknown; permalink verification required"

        confirmation_seen = False
        permalink: str | None = None
        for attempt in range(3):
            try:
                li_run(
                    ["wait", "3000" if attempt == 0 else "1500"],
                    port=port,
                    timeout=10,
                )
                snap = li_run(
                    ["snapshot", "-i"], port=port, timeout=30
                )
                if not snap.ok:
                    continue
                snapshot_text = snap.stdout or ""
                confirmation_seen = confirmation_seen or bool(
                    re.search(
                        r"Post successful|View post",
                        snapshot_text,
                        re.IGNORECASE,
                    )
                )
                view_match = re.search(
                    r'(?:link|button) "View post" \[ref=(e\d+)\]',
                    snapshot_text,
                    re.IGNORECASE,
                )
                if not view_match:
                    continue
                href = li_run(
                    ["get", "attr", view_match.group(1), "href"],
                    port=port,
                    timeout=20,
                )
                if href.ok:
                    permalink = _validated_linkedin_permalink(href.output)
                if permalink:
                    break
            except Exception:  # noqa: BLE001 - click already crossed write boundary
                continue

        if permalink:
            if publisher:
                # Open only the verified View post URL. A toast/permalink alone
                # cannot prove that the approved company, caption and media won.
                try:
                    bound_paths.add(urlsplit(permalink).path.rstrip("/"))
                    navigated = li_run(["open", permalink], port=port, timeout=45)
                    proof = {}
                    if navigated.ok:
                        # LinkedIn may redirect a share permalink to an activity
                        # permalink. Pin the same tab, accept only its validated
                        # public post URL, then continue reading that exact tab.
                        raw_run(["tab", bound_tab_id], port=port, timeout=20)
                        actual_url = raw_run(["get", "url"], port=port, timeout=20)
                        canonical = _validated_linkedin_permalink(actual_url.stdout or "")
                        if not actual_url.ok or not canonical:
                            raise ValueError("company post did not resolve to a public permalink")
                        bound_paths.add(urlsplit(canonical).path.rstrip("/"))
                        li_run(["wait", "--load", "domcontentloaded"], port=port, timeout=30)
                        for _ in range(3):
                            li_run(["wait", "1500"], port=port, timeout=8)
                            proof = _browser_json(li_run(
                                ["eval", _company_post_probe(canonical)], port=port, timeout=20,
                            ))
                            if _valid_company_post_proof(
                                proof, publisher, body, expected_logo=expected_company_logo,
                            ):
                                break
                    if not _valid_company_post_proof(
                        proof, publisher, body, expected_logo=expected_company_logo,
                    ):
                        self._last_verification.update({
                            "post_url": permalink,
                            "confirmation_result": "company_post_content_or_publisher_unverified",
                        })
                        return True, (
                            "company post submitted; publisher, caption and media "
                            "verification required"
                        )
                except Exception:
                    self._last_verification.update({
                        "post_url": permalink,
                        "confirmation_result": "company_post_verification_failed",
                    })
                    return True, "company post submitted; verification required"
                caption_proof.update({
                    "publisher_verified_after_submit": "true",
                    "caption_verified_after_submit": "true",
                    "media_verified_after_submit": "true",
                    "published_caption_sha256": hashlib.sha256(expected_text.encode()).hexdigest(),
                })
            self._last_verification = {
                **caption_proof,
                "verification_state": "verified",
                "post_url": permalink,
                "submitted_at": submitted_at,
                "confirmation_result": "view_post_permalink_verified",
            }
            return True, "post submitted and confirmed"

        self._last_verification = {
            **caption_proof,
            "verification_state": "verification_required",
            "post_url": "",
            "submitted_at": submitted_at,
            "confirmation_result": (
                "confirmation_seen_without_permalink"
                if confirmation_seen
                else "submit_clicked_without_confirmation"
            ),
        }
        return True, "post submitted; permalink verification required"

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

        if workflow_id == "linkedin.post.create":
            data = capture_browser_screenshot_png(
                port=port, session=_LINKEDIN_BROWSER_SESSION,
            )
        else:
            data = capture_browser_screenshot_png(port=port)
        out_dir = self._screenshot_dir or (_data_dir() / "browser_writes")
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        out_path = out_dir / f"{ts}-{_safe_workflow_slug(workflow_id)}.png"
        out_path.write_bytes(data)
        return str(out_path)

    def verification_receipt(self) -> dict[str, str]:
        """Return public-safe proof metadata for the most recent drive."""

        return dict(self._last_verification)

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
