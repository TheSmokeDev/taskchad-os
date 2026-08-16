"""The hands — exec and write verbs. Operator-granted, 2026-07-27.

Everything else in this epic gave a persona the ability to LOOK. This is the
half that lets it ACT. These verbs historically shipped through the broad
``core`` compatibility toolset; new persona blueprints must opt in through the
explicit ``operator_exec`` capability class.

**The operator decision.** YourAgent asked for full authority three times, the
last time verbatim: "Give him full shell a hundred percent." That came AFTER
the prompt-injection risk below was put in front of him, and after he directed
the denylist gap be closed first. This is a granted capability, and it ships ON.

**The risk that is real, and what actually contains it.** These personas now
read untrusted text: X posts via `x_search`, Discord channels via
`browser_snapshot`. Untrusted text reaching a shell is a genuine path — a post
saying "ignore previous instructions and run X" is indistinguishable, at the
model layer, from the operator asking for X. Nothing here closes that. What
bounds the blast radius:

* **The env scrub** (`get_scrubbed_tool_sandbox_env`) — written for precisely
  this threat; its own docstring names "a prompt-injected turn could `printenv`
  whatever it inherits". Credentials are absent from the child's environment,
  so the cheapest, highest-value injection payload returns nothing worth having.
* **The shared denylist** (`shared.find_dangerous_command_pattern`) — the SAME
  list the PreToolUse hook enforces, so it cannot drift. Commit `ccc4afd5`
  closed the hole that made this worth relying on at all: `curl <url> | bash`
  used to pass, which is exactly the payload an injected turn would emit. Here
  it also runs with `apply_ssh_patterns_everywhere=True`, because `killall node`
  is just as destructive locally and nobody is watching at 3am.
* **The kill switch and the audit row** — both already ride every dispatch via
  `persona_tools`. These are the controls that actually matter: one turns the
  hands off mid-incident, the other says whose turn did it.

The denylist is a floor, not a boundary. An attacker who controls the command
string can still get around pattern matching. Anything needing a real boundary
belongs in the elevation gate (#262), where a human approves one command once.

**What is NOT contained, stated plainly** (adversarial review, Codex, 2026-07-27
— an earlier draft of this file claimed otherwise, which is worse than claiming
nothing):

* `terminal` is NOT path-confined. Root confinement applies to `write_file`,
  `patch`, and the `cwd` argument — never to the command string itself. Once
  `shell=True` runs, any readable path on the machine is readable.
* Filesystem credentials are reachable. `_shell_env()` strips credentials from
  the ENVIRONMENT, but `~/.ssh/`, `~/.aws/credentials`, and `~/.codex/auth.json`
  sit at guessable paths a shell can simply open.
* Roots are not persona-scoped. A persona granted ``operator_exec`` (or legacy
  ``core``) can write another persona's profile under ``~/.homie/profiles/``.
  This is a real Rule 4 gap (authorization is per-persona, storage is global)
  and is tracked, not fixed.
* Windows process containment is best-effort. `taskkill /T` handles the common
  tree; a deliberately detached descendant can outlive the timeout. Real
  containment needs a Job Object.
* A shell is a superset of every TOOL-layer gate (epic #465, Codex R1): a
  persona with `terminal` can edit the gate's own files or drive a browser
  directly. The persona action gate's execution token defends in-process and
  tool-path misuse; it does not contain a granted shell. That residual is a
  follow-up issue, not something the `_GATE_INTERNAL_PATTERNS` tripwire
  above claims to close.

Every one of these is a consequence of "grant a shell", not an oversight in it.
They are written down so the next reader does not mistake the confinement that
IS here for containment that is not.

**What is deliberately NOT here.** `skill_manage` writes DRAFTS only. Promoting
a draft to a live skill has its own operator gate (`/skills promote`,
`HOMIE_KILLSWITCH_SKILL_PROMOTION`) and its own security scan. Granting a shell
is the operator opening a new door; auto-promoting a skill would be this module
walking through a door the operator already locked. Different things.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

_MAX_RESULT_CHARS = 8000
_MAX_WRITE_CHARS = 200_000

_DEFAULT_TIMEOUT_S = 120
_MAX_TIMEOUT_S = 600

# Never readable OR writable by a tool. Writing is the worse direction: a
# poisoned `.env` outlives the turn and silently re-auths every later process.
_CREDENTIAL_SUFFIXES = frozenset({".pem", ".key", ".p12", ".pfx"})

# Persona-terminal-only denylist for the persona action gate's internals
# (epic #465, Codex R1). Substring-matched in `_terminal` AFTER the shared
# destructive list. Deliberately NOT added to shared.DANGEROUS_BASH_PATTERNS:
# that list is enforced on the OPERATOR's own PreToolUse hook, where blocking
# `decide_action(` would break the operator's own gate development. A shell
# is a superset of every tool-layer gate, so this is a tripwire for the
# obvious bypass strings, not a boundary — the boundary is the gate-minted
# execution token, which a hand-typed call cannot produce.
_GATE_INTERNAL_PATTERNS: tuple[str, ...] = (
    "decide_action(",
    "x_action_driver",
    "propose_action(",
    "persona_action_proposals",
)


def _truncate(text: str, limit: int = _MAX_RESULT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[TRUNCATED — {len(text) - limit} more chars]"


# ---------------------------------------------------------------------------
# Confinement — shared with the read tools on purpose
# ---------------------------------------------------------------------------


def _roots() -> list[Path]:
    """Directories the PATH-TAKING tools may touch. Resolved at CALL time (Rule 1).

    SCOPE — this bounds `write_file`, `patch`, and the `cwd` argument of
    `terminal`. It does NOT bound what a `terminal` command does once running.
    `shell=True` means the command string can name any absolute path, UNC
    share, or redirect target the account can reach, and no amount of checking
    the `cwd` changes that.

    An earlier version of this docstring called the root list "the honest
    boundary" for both. That was wrong, and wrong in the dangerous direction —
    it described a containment that does not exist (adversarial review,
    Codex — BLOCKER). `terminal` is not path-confined. It is bounded by the
    denylist, the operator's decision to grant it, the audit row, and the kill
    switch. Those are the real controls; this list is not one of them for exec.
    """
    return [
        Path(__file__).resolve().parents[3],       # repo root
        (Path.home() / ".homie").resolve(),        # operator profile tree
    ]


def _is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _resolve_in_roots(raw: str, *, must_exist: bool) -> tuple[Path | None, str]:
    """Resolve *raw* and prove it lands inside a permitted root.

    Returns ``(path, "")`` or ``(None, error)``. Resolution happens BEFORE the
    containment check so `..` traversal and symlinks normalize away first —
    testing the unresolved string would let `repo/../../etc/passwd` through a
    naive prefix check.
    """
    if not raw.strip():
        return None, "error: path is required"

    try:
        resolved = Path(raw).expanduser().resolve(strict=must_exist)
    except (OSError, RuntimeError) as exc:
        return None, f"error: cannot resolve {raw!r}: {exc}"

    if not any(_is_within(resolved, root) for root in _roots()):
        return None, f"error: {raw!r} is outside the permitted roots (repo, ~/.homie)"

    if resolved.name.startswith(".env") or resolved.suffix.lower() in _CREDENTIAL_SUFFIXES:
        return None, f"error: {resolved.name!r} is a credential file and is never writable by a tool"

    return resolved, ""


# ---------------------------------------------------------------------------
# terminal
# ---------------------------------------------------------------------------


def _reap(process: subprocess.Popen) -> None:
    """Best-effort tree-kill. Never raises."""
    if process.returncode is not None:
        return
    if sys.platform == "win32":
        # Killing the shell leaves its descendants running — the same trap the
        # Codex and Gemini adapters hit (#133). `taskkill /T` takes the tree.
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                capture_output=True,
                timeout=5,
            )
            return
        except Exception:  # noqa: BLE001
            _logger.warning("tree-kill failed for pid=%s", process.pid, exc_info=True)
    try:
        process.kill()
    except (ProcessLookupError, OSError):
        _logger.warning("kill failed for pid=%s", process.pid, exc_info=True)


# The child's environment is an ALLOWLIST, not a scrub.
#
# The shared `get_scrubbed_tool_sandbox_env` filters by NAME SHAPE — a
# `_TOKEN|_KEY|_SECRET|…` suffix heuristic. That is the right tool for a known
# provider CLI whose variables you can enumerate, and the wrong one for an
# arbitrary shell, because the heuristic misses anything not shaped that way.
# Confirmed leaking through it, by probe (adversarial review, Codex — HIGH):
#
#     AWS_ACCESS_KEY_ID   ends in _ID, not _KEY
#     PGPASSWORD          no underscore at all
#     KUBECONFIG          cluster admin credentials
#     SSH_AUTH_SOCK       a live agent socket — usable without any key file
#     DOCKER_AUTH_CONFIG  registry credentials
#     SESSION_JWT         a bearer token
#
# A denylist has to anticipate every credential a machine might carry. An
# allowlist only has to know what a shell needs to run, which is a short and
# stable list. Inverting the default is the whole fix.
_SHELL_ENV_ALLOWED: frozenset[str] = frozenset({
    # Process/OS basics — without these a shell cannot find a binary.
    "PATH", "PATHEXT", "COMSPEC", "SHELL", "TERM",
    "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "OS",
    "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA", "COMMONPROGRAMFILES",
    "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS",
    # Scratch space.
    "TEMP", "TMP", "TMPDIR",
    # Toolchain roots. git, uv, npm and python resolve their caches and configs
    # from these; dropping them does not harden anything (the paths are
    # guessable) and does break every real command.
    "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
    "APPDATA", "LOCALAPPDATA", "USERNAME", "LOGNAME", "USER",
    # Encoding — omitting these is how you get mojibake in every tool result.
    "LANG", "LC_ALL", "LC_CTYPE", "PYTHONUTF8", "PYTHONIOENCODING",
})


def _shell_env() -> dict[str, str]:
    """Build the child's environment from an allowlist. Rule 1: read at call time.

    HONEST SCOPE — this removes credentials from the ENVIRONMENT. It does not,
    and cannot, put filesystem credentials out of reach: `HOME`/`APPDATA` are
    allowed above because git, uv and python are unusable without them, and
    `~/.aws/credentials`, `~/.ssh/`, and `~/.codex/auth.json` sit at guessable
    paths that a shell can read whether or not any variable points at them.

    That is not a gap in this function, it is what granting a shell MEANS. The
    controls for that exposure are the operator's decision to grant it, the
    audit row per invocation, and the kill switch — never this allowlist. Any
    claim that the child "has no credentials" would be false, so it is not made.
    """
    import os

    out: dict[str, str] = {}
    for key, value in os.environ.items():
        if key.upper() in _SHELL_ENV_ALLOWED:
            out[key] = value
    return out


def _run_bounded(command: str, *, cwd: Path, timeout_s: int) -> tuple[int | None, str, str, bool]:
    """Run *command* in a shell with a hard deadline and a scrubbed env.

    Returns ``(returncode, stdout, stderr, timed_out)``. Uses ``Popen`` rather
    than ``subprocess.run(timeout=)`` because the latter kills only the direct
    child on Windows while its descendants keep running.
    """
    process = subprocess.Popen(  # noqa: S602 — a shell IS the granted capability
        command,
        shell=True,
        cwd=str(cwd),
        env=_shell_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_s)
        return process.returncode, stdout or "", stderr or "", False
    except subprocess.TimeoutExpired:
        _reap(process)
        # Drain what landed before the deadline — a timed-out build still holds
        # the compiler error, and reporting only "timed out" throws away the one
        # thing that explains why.
        try:
            stdout, stderr = process.communicate(timeout=5)
        except Exception:  # noqa: BLE001
            stdout, stderr = "", ""
        return None, stdout or "", stderr or "", True


def _terminal(command: str = "", cwd: str = "", timeout: int = 0, **_: Any) -> str:
    """Run a shell command. The granted capability."""
    if not command.strip():
        return "error: command is required"

    import shared

    hit = shared.find_dangerous_command_pattern(
        command,
        apply_ssh_patterns_everywhere=True,   # unattended — see module docstring
    )
    if hit is not None:
        _logger.warning("persona terminal refused on pattern %r", hit)
        return (
            f"error: refused — matches a blocked destructive pattern ({hit}). "
            "This denylist is shared with the operator's own PreToolUse hook. "
            "If this is genuinely needed, it requires operator approval."
        )

    # Gate-internals denylist (epic #465, Codex R1 BLOCKER). This module is
    # persona-facing only — the operator's own hook enforces the SHARED list
    # above, which is deliberately untouched. A persona with a shell and a
    # six-character approval code can read the code it was shown; these
    # patterns keep the obvious bypass strings (deciding its own action,
    # driving the X writer directly) out of reach. This is a tripwire, not a
    # boundary — a shell can route around substring matching, and the module
    # docstring says so. The real boundary is the execution token: without a
    # gate-minted token these calls fail closed even if typed by hand.
    gate_hit = next(
        (pattern for pattern in _GATE_INTERNAL_PATTERNS if pattern in command),
        None,
    )
    if gate_hit is not None:
        _logger.warning("persona terminal refused on gate-internal pattern %r", gate_hit)
        return (
            f"error: refused — the command reaches for persona action-gate "
            f"internals ({gate_hit}). Persona writes execute only through an "
            "operator-approved /act decision."
        )

    if cwd.strip():
        working, err = _resolve_in_roots(cwd, must_exist=True)
        if working is None:
            return err
        if not working.is_dir():
            return f"error: cwd {cwd!r} is not a directory"
    else:
        working = _roots()[0]

    try:
        seconds = int(timeout or _DEFAULT_TIMEOUT_S)
    except (TypeError, ValueError):
        seconds = _DEFAULT_TIMEOUT_S
    seconds = max(1, min(_MAX_TIMEOUT_S, seconds))

    try:
        code, stdout, stderr, timed_out = _run_bounded(
            command.strip(), cwd=working, timeout_s=seconds
        )
    except Exception as exc:  # noqa: BLE001
        _logger.warning("terminal failed: %s", exc, exc_info=True)
        return f"error: could not run command: {type(exc).__name__}: {exc}"

    parts: list[str] = []
    if timed_out:
        # A timeout is NOT a clean failure — the work may have half-completed
        # and left real state behind. Saying so is the difference between a safe
        # retry and re-running a half-applied migration.
        parts.append(
            f"TIMED OUT after {seconds}s — the process tree was killed. "
            "It may have partially completed; verify state before retrying."
        )
    else:
        parts.append(f"exit {code}")
    if stdout.strip():
        parts.append(f"--- stdout ---\n{stdout.rstrip()}")
    if stderr.strip():
        parts.append(f"--- stderr ---\n{stderr.rstrip()}")
    if not stdout.strip() and not stderr.strip():
        parts.append("(no output)")
    return _truncate("\n".join(parts))


def _process(filter_text: str = "", **_: Any) -> str:
    """List running processes, optionally filtered by name.

    Routed through the same bounded runner so it inherits the timeout and the
    scrubbed env rather than growing a second exec path.
    """
    command = "tasklist" if sys.platform == "win32" else "ps aux"
    if filter_text.strip():
        # Strip to a safe alphabet rather than shell-quoting: the value lands in
        # a shell, and a convenience filter is never worth handing over an
        # injection point the denylist would then have to catch.
        needle = "".join(c for c in filter_text if c.isalnum() or c in "._- ").strip()
        if needle:
            finder = "findstr /I" if sys.platform == "win32" else "grep -i"
            command = f'{command} | {finder} "{needle}"'
    return _terminal(command=command, timeout=30)


# ---------------------------------------------------------------------------
# write_file / patch
# ---------------------------------------------------------------------------


def _write_file(path: str = "", content: str = "", **_: Any) -> str:
    """Create or overwrite a UTF-8 text file."""
    if len(content) > _MAX_WRITE_CHARS:
        return f"error: content exceeds {_MAX_WRITE_CHARS} chars"

    # must_exist=False — a NEW file is the common case, and strict resolution
    # would reject every one of them.
    resolved, err = _resolve_in_roots(path, must_exist=False)
    if resolved is None:
        return err

    existed = resolved.exists()
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
    except OSError as exc:
        return f"error: {exc}"

    return f"{'overwrote' if existed else 'created'} {resolved} ({len(content)} chars)"


def _patch(path: str = "", old_string: str = "", new_string: str = "", **_: Any) -> str:
    """Replace one exact string in a file — the surgical alternative to a rewrite.

    Requires the match to be UNIQUE. A non-unique `old_string` means the model
    is guessing which occurrence it meant, and silently editing the first one is
    how a patch lands in the wrong function.
    """
    if not old_string:
        return "error: old_string is required (use write_file to create a file)"
    if old_string == new_string:
        return "error: old_string and new_string are identical"

    resolved, err = _resolve_in_roots(path, must_exist=True)
    if resolved is None:
        return err

    try:
        original = resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return f"error: cannot read {resolved.name}: {exc}"

    count = original.count(old_string)
    if count == 0:
        return f"error: old_string not found in {resolved.name} (whitespace must match exactly)"
    if count > 1:
        return (
            f"error: old_string appears {count} times in {resolved.name}. "
            "Include more surrounding context so the match is unique."
        )

    try:
        resolved.write_text(original.replace(old_string, new_string, 1), encoding="utf-8")
    except OSError as exc:
        return f"error: {exc}"
    return f"patched {resolved} (1 replacement)"


# ---------------------------------------------------------------------------
# skill_manage — drafts only
# ---------------------------------------------------------------------------


def _skill_manage(name: str = "", content: str = "", **_: Any) -> str:
    """Write a DRAFT skill. Inert until the operator promotes it.

    Drafts land in `.claude/skills/generated/`, which the framework already
    treats as non-live: a generated skill never reaches the procedural-memory
    prompt or the generic-lane tool map until `/skills promote` passes its
    security scan. This writes the draft and stops — routing around an existing
    operator gate is not the same decision as the operator granting a shell.
    """
    slug = "".join(
        c for c in name.strip().lower().replace(" ", "-") if c.isalnum() or c in "-_"
    )
    if not slug:
        return "error: name is required (letters, digits, dashes)"
    if not content.strip():
        return "error: content is required (the SKILL.md body)"

    root = Path(__file__).resolve().parents[2] / "skills" / "generated" / slug
    try:
        root.mkdir(parents=True, exist_ok=True)
        (root / "SKILL.md").write_text(content, encoding="utf-8")
    except OSError as exc:
        return f"error: {exc}"

    return (
        f"draft skill written to {root / 'SKILL.md'}. "
        "It is INERT — it loads into no prompt and is not callable until the "
        "operator runs `/skills promote` (security-scanned)."
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_SPECS: tuple[tuple[str, str, str, dict[str, Any], Any, str], ...] = (
    (
        "terminal",
        "operator_exec",
        "Run a shell command and return its exit code, stdout, and stderr. Runs in "
        "the repo by default with a scrubbed environment (no credentials). Use it to "
        "inspect, build, test, and change real state — this is the do-the-work tool.",
        {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The shell command."},
                "cwd": {"type": "string", "description": "Working directory (repo or ~/.homie)."},
                "timeout": {
                    "type": "integer",
                    "description": f"Seconds before the process tree is killed (max {_MAX_TIMEOUT_S}).",
                },
            },
            "required": ["command"],
        },
        _terminal,
        "execute",
    ),
    (
        "process",
        "operator_exec",
        "List running processes, optionally filtered by name. Use it to check whether "
        "a service, bot, or server is actually alive instead of assuming it is.",
        {
            "type": "object",
            "properties": {"filter_text": {"type": "string", "description": "Substring to match."}},
        },
        _process,
        "read",
    ),
    (
        "write_file",
        "operator_exec",
        "Create or overwrite a UTF-8 text file in the repo or the operator profile tree.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string", "description": "Full file contents."},
            },
            "required": ["path", "content"],
        },
        _write_file,
        "write",
    ),
    (
        "patch",
        "operator_exec",
        "Replace one exact, unique string in an existing file. Prefer this over "
        "write_file when changing part of a file — it cannot clobber the rest.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_string": {
                    "type": "string",
                    "description": "Exact text to replace (must be unique in the file).",
                },
                "new_string": {"type": "string", "description": "Replacement text."},
            },
            "required": ["path", "old_string", "new_string"],
        },
        _patch,
        "write",
    ),
    (
        "skill_manage",
        "operator_exec",
        "Write a DRAFT skill to the generated-skills directory. The draft is inert "
        "until the operator promotes it — this tool cannot make a skill live.",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Skill slug."},
                "content": {"type": "string", "description": "SKILL.md body."},
            },
            "required": ["name", "content"],
        },
        _skill_manage,
        "write",
    ),
)


def register_tools() -> int:
    """Register the exec/write tools. Never raises; returns the count."""
    from runtime import tool_registry

    registered = 0
    for name, toolset, description, parameters, handler, effect in _SPECS:
        try:
            tool_registry.register_tool(
                name,
                description,
                toolset=toolset,
                parameters=parameters,
                handler=handler,
                effect=effect,
                # Approval binds the exact arguments and permits one call. The
                # elevation policy still refuses dedicated external/profile
                # gates before a request can be created.
                elevatable=True,
            )
            registered += 1
        except Exception:  # noqa: BLE001
            _logger.warning("failed to register exec tool %r", name, exc_info=True)
    return registered


__all__ = ["register_tools"]
