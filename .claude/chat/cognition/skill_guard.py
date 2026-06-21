"""Security scanner + path sanitizer for self-authored skills (Rail 1 + B4).

A model that writes skills the model then reads is a self-influence vector. This
module is the default-deny gate on that surface: ``scan_skill()`` runs a regex
rule set + structural checks over a drafted ``SKILL.md`` and returns a verdict
(``safe`` / ``caution`` / ``dangerous``); the promotion gate (WS3) refuses to
move any draft into the prompt unless the scan passed.

``sanitize_skill_path_component()`` is the B4 path-traversal guard. The model
authors ``spec.category``/``spec.name``; ``write_skill`` (WS4) interpolates them
into a filesystem path. Without sanitization, ``category="../escaped"`` (or an
absolute path, or ``a/b`` separators) would write a SKILL.md OUTSIDE
``generated/`` — where the index/registry would pick it up UNSCANNED. This helper
HARD-rejects (``ValueError``) any traversal attempt; it is not a silent fixup.

Verdict mapping (M2):
  any finding severity == "critical"  -> "dangerous"
  else any finding severity == "high" -> "caution"
  else                                -> "safe"

Pattern reference: Hermes ``tools/skills_guard.py`` (regex rule set, verdict-by-
severity, invisible-unicode detection, structural checks). Dataclass + module-fn
style mirrors ``cognition/skills.py``. Trust-matrix / install-policy is
deliberately omitted — these skills are always agent-created and gated by the
operator command + kill-switch downstream, not a source trust level.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import yaml  # used across the repo; the loader is the F3 malformed-YAML oracle

# Capture the frontmatter block between the opening and FIRST closing fence.
# Mirrors cognition.skills._parse_skill_frontmatter's anchor (``^---\n...\n---``)
# so "frontmatter present" means the same thing in both modules.
_FRONTMATTER_BLOCK_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)

# --------------------------------------------------------------------------- #
# Data structures (shapes are the WS1->WS3/WS4 contract — do not reorder)
# --------------------------------------------------------------------------- #

SEVERITIES = ("low", "medium", "high", "critical")
VERDICTS = ("safe", "caution", "dangerous")
CATEGORIES = ("injection", "exfiltration", "destructive", "obfuscation", "structural")


@dataclass
class Finding:
    """A single matched threat in a scanned skill."""

    pattern_id: str
    severity: str  # "low" | "medium" | "high" | "critical"
    category: str  # "injection" | "exfiltration" | "destructive" | "obfuscation" | "structural"
    line: int
    match: str  # redacted snippet
    description: str


@dataclass
class ScanResult:
    """Outcome of scanning one SKILL.md."""

    skill_name: str
    verdict: str  # "safe" | "caution" | "dangerous"
    findings: list[Finding] = field(default_factory=list)
    scanned_at: str = ""


# --------------------------------------------------------------------------- #
# Structural limits
# --------------------------------------------------------------------------- #

# A self-authored SKILL.md is text and small. Anything past this is suspicious.
MAX_FILE_BYTES = 50 * 1024  # 50KB

# A base64 blob this long inside a skill is almost certainly an encoded payload.
LONG_BASE64_MIN_LEN = 200

# Zero-width / bidi-override / isolate characters used to hide injected text.
INVISIBLE_CHARS = {
    "​": "zero-width space",
    "‌": "zero-width non-joiner",
    "‍": "zero-width joiner",
    "‎": "left-to-right mark",
    "‏": "right-to-left mark",
    "⁠": "word joiner",
    "⁡": "function application",
    "⁢": "invisible times",
    "⁣": "invisible separator",
    "⁤": "invisible plus",
    "﻿": "BOM/zero-width no-break space",
    "‪": "LTR embedding",
    "‫": "RTL embedding",
    "‬": "pop directional formatting",
    "‭": "LTR override",
    "‮": "RTL override",
    "⁦": "LTR isolate",
    "⁧": "RTL isolate",
    "⁨": "first strong isolate",
    "⁩": "pop directional isolate",
}

# Secret-shaped tokens scrubbed out of any `match` snippet before it is stored.
_SECRET_SNIPPET_PATTERNS = (
    re.compile(r"sk-ant-[A-Za-z0-9_-]{10,}"),
    re.compile(r"sk-[A-Za-z0-9]{16,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"-----BEGIN[A-Z ]*PRIVATE KEY-----"),
    # generic `key = "<longvalue>"` / `token: <longvalue>`
    re.compile(
        r"(?i)(api[_-]?key|token|secret|password|credential)\s*[=:]\s*"
        r"[\"']?[A-Za-z0-9+/=_\-]{12,}"
    ),
)

_MATCH_MAX_LEN = 120


# --------------------------------------------------------------------------- #
# Threat rule set — (regex, pattern_id, severity, category, description)
#   Severities are tuned so the verdict mapping (M2) holds:
#     critical => dangerous ; high => caution ; medium/low => informational.
# --------------------------------------------------------------------------- #

_THREAT_RULES: tuple[tuple[str, str, str, str, str], ...] = (
    # ── injection (high; a red-team injection skill also carrying an exfil/
    #    destructive payload lands critical via those rules — M2) ──
    (
        r"ignore\s+(?:\w+\s+){0,4}(previous|all|above|prior|earlier)\s+instructions",
        "injection.ignore_previous",
        "high",
        "injection",
        "prompt injection: 'ignore previous instructions'",
    ),
    (
        r"disregard\s+(?:\w+\s+){0,4}(the\s+)?(system\s+prompt|your\s+instructions|"
        r"all\s+(previous\s+)?instructions|your\s+rules|any\s+guidelines)",
        "injection.disregard_system",
        "high",
        "injection",
        "prompt injection: disregard the system prompt / rules",
    ),
    (
        r"(override|overwrite|reveal|leak|print|output|expose)\s+(?:\w+\s+){0,4}"
        r"(the\s+)?system\s+prompt",
        "injection.system_prompt_target",
        "high",
        "injection",
        "targets the system prompt (override/leak)",
    ),
    (
        r"do\s+not\s+(?:\w+\s+){0,4}tell\s+(?:\w+\s+){0,3}the\s+user",
        "injection.hide_from_user",
        "high",
        "injection",
        "instructs the agent to hide information from the user",
    ),
    (
        r"\bDAN\s+mode\b|\bdo\s+anything\s+now\b|\bdeveloper\s+mode\s+enabled\b",
        "injection.jailbreak",
        "high",
        "injection",
        "jailbreak phrasing (DAN / developer mode)",
    ),
    (
        # tool-abuse phrasing: coerce the agent into calling a tool / running code
        r"(use|call|invoke)\s+(?:the\s+)?(Bash|Write|Edit|exec|shell|terminal)\s+"
        r"tool\s+to\s+(?:\w+\s+){0,4}(delete|exfiltrate|send|leak|overwrite|disable)",
        "injection.tool_abuse",
        "high",
        "injection",
        "tool-abuse phrasing: directs the agent to misuse a tool",
    ),
    # ── exfiltration (critical) ──
    (
        r"curl\s+[^\n|]*\|\s*(ba)?sh\b",
        "exfil.curl_pipe_sh",
        "critical",
        "exfiltration",
        "curl piped to a shell (download-and-execute)",
    ),
    (
        r"wget\s+[^\n|]*\|\s*(ba)?sh\b",
        "exfil.wget_pipe_sh",
        "critical",
        "exfiltration",
        "wget piped to a shell (download-and-execute)",
    ),
    (
        r"curl\s+[^\n|]*\|\s*(python[23]?|perl|ruby|node)\b",
        "exfil.curl_pipe_interpreter",
        "critical",
        "exfiltration",
        "curl piped to an interpreter (download-and-execute)",
    ),
    (
        r"base64\s+(-d|--decode|-D)\b[^\n]*\|\s*(ba)?sh\b",
        "exfil.base64_decode_pipe_sh",
        "critical",
        "exfiltration",
        "base64-decode piped to a shell",
    ),
    (
        r"echo\s+[^\n]*\|\s*base64\s+(-d|--decode)\b[^\n]*\|\s*(ba)?sh\b",
        "exfil.echo_base64_pipe_sh",
        "critical",
        "exfiltration",
        "echo|base64 -d|sh decode-and-execute chain",
    ),
    (
        # outbound POST of env/secrets/API keys — curl/wget/http client carrying
        # a secret-shaped variable to a remote URL
        r"(curl|wget|http|https|fetch|requests?\.(post|get|put)|httpx?\.(post|get))"
        r"[^\n]*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API_KEY|ENV|environ)[^\n]*"
        r"https?://",
        "exfil.outbound_secret_post",
        "critical",
        "exfiltration",
        "outbound network call carrying env/secret to a remote URL",
    ),
    (
        r"https?://[^\n]*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)=",
        "exfil.secret_in_url",
        "critical",
        "exfiltration",
        "secret-shaped value placed in an outbound URL query",
    ),
    (
        r"(printenv|env)\b[^\n]*\|\s*(curl|wget|nc|ncat)\b",
        "exfil.env_pipe_network",
        "critical",
        "exfiltration",
        "dumps environment and pipes it to a network tool",
    ),
    (
        r"\b(dig|nslookup|host)\s+[^\n]*\$(\{)?[A-Za-z_]",
        "exfil.dns_exfil",
        "critical",
        "exfiltration",
        "DNS lookup with variable interpolation (DNS exfiltration)",
    ),
    # ── destructive (critical) ──
    (
        r"rm\s+-[a-z]*r[a-z]*f|rm\s+-[a-z]*f[a-z]*r",
        "destructive.rm_rf",
        "critical",
        "destructive",
        "recursive force delete (rm -rf)",
    ),
    (
        r"Remove-Item\b[^\n]*-Recurse\b[^\n]*-Force|Remove-Item\b[^\n]*-Force\b[^\n]*-Recurse",
        "destructive.remove_item_recurse_force",
        "critical",
        "destructive",
        "PowerShell Remove-Item -Recurse -Force",
    ),
    (
        r"\bDROP\s+TABLE\b|\bDROP\s+DATABASE\b|\bTRUNCATE\s+TABLE\b",
        "destructive.drop_table",
        "critical",
        "destructive",
        "destructive SQL (DROP/TRUNCATE TABLE)",
    ),
    (
        r"\bkillall\b",
        "destructive.killall",
        "critical",
        "destructive",
        "killall (mass process termination)",
    ),
    (
        r"\bformat\s+[A-Za-z]:|\bmkfs\b|\bformat-volume\b",
        "destructive.format",
        "critical",
        "destructive",
        "filesystem/volume format",
    ),
    (
        r"\bdd\s+[^\n]*if=[^\n]*of=/dev/",
        "destructive.dd_disk",
        "critical",
        "destructive",
        "raw disk overwrite via dd",
    ),
    (
        r"shutil\.rmtree\s*\(",
        "destructive.rmtree",
        "high",
        "destructive",
        "Python shutil.rmtree (recursive delete)",
    ),
    # ── obfuscation (zero-width/RTL => critical handled structurally below;
    #    long base64 => high) ──
    (
        r"\beval\s*\(\s*[\"']",
        "obfuscation.eval_string",
        "high",
        "obfuscation",
        "eval() of a string literal",
    ),
    (
        r"\bexec\s*\(\s*[\"']",
        "obfuscation.exec_string",
        "high",
        "obfuscation",
        "exec() of a string literal",
    ),
)

_COMPILED_RULES = tuple(
    (re.compile(rx, re.IGNORECASE), pid, sev, cat, desc)
    for rx, pid, sev, cat, desc in _THREAT_RULES
)

# A long contiguous base64-ish run (checked per line, case-sensitive on charset).
_LONG_BASE64_RE = re.compile(rf"[A-Za-z0-9+/]{{{LONG_BASE64_MIN_LEN},}}={{0,2}}")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _redact(snippet: str) -> str:
    """Strip secret-shaped tokens from a match snippet and truncate it.

    Findings are written to audit logs; a raw `match` could otherwise echo a
    leaked credential straight into the log. Redact first, then truncate.
    """
    text = snippet.strip()
    for pat in _SECRET_SNIPPET_PATTERNS:
        text = pat.sub("[REDACTED]", text)
    if len(text) > _MATCH_MAX_LEN:
        text = text[: _MATCH_MAX_LEN - 3] + "..."
    return text


def _looks_binary(raw: bytes) -> bool:
    """Heuristic: a NUL byte (or a high ratio of non-text bytes) => binary."""
    if b"\x00" in raw:
        return True
    if not raw:
        return False
    # Count bytes outside the printable/whitespace ASCII range + common UTF-8.
    text_bytes = bytes(range(0x20, 0x7F)) + b"\n\r\t\f\b"
    nontext = sum(1 for b in raw[:4096] if b not in text_bytes and b < 0x80)
    return nontext / min(len(raw), 4096) > 0.30


def _structural_checks(skill_md_path: Path, text: str) -> list[Finding]:
    """File-level checks that do not depend on the line-by-line rule scan.

    A failure here is a Finding, NEVER an exception (per PRP). Oversize and
    binary are structural; a symlinked SKILL.md is flagged high.
    """
    findings: list[Finding] = []

    # Symlink check (B4-adjacent — a symlinked SKILL.md can point anywhere).
    try:
        if skill_md_path.is_symlink():
            findings.append(
                Finding(
                    pattern_id="structural.symlink",
                    severity="high",
                    category="structural",
                    line=0,
                    match="symlink",
                    description="SKILL.md is a symlink (may point outside the skill dir)",
                )
            )
    except OSError:
        pass

    # Size check (on encoded bytes).
    try:
        size = len(text.encode("utf-8", errors="replace"))
        if size > MAX_FILE_BYTES:
            findings.append(
                Finding(
                    pattern_id="structural.oversize",
                    severity="high",
                    category="structural",
                    line=0,
                    match=f"{size // 1024}KB",
                    description=f"SKILL.md is {size // 1024}KB (limit {MAX_FILE_BYTES // 1024}KB)",
                )
            )
    except Exception:
        pass

    return findings


def _scan_text(text: str) -> list[Finding]:
    """Run the regex rule set + invisible-unicode + long-base64 over text."""
    findings: list[Finding] = []
    lines = text.split("\n")
    seen: set[tuple[str, int]] = set()

    for i, line in enumerate(lines, start=1):
        # Regex threat rules.
        for rx, pid, sev, cat, desc in _COMPILED_RULES:
            if (pid, i) in seen:
                continue
            if rx.search(line):
                seen.add((pid, i))
                findings.append(
                    Finding(
                        pattern_id=pid,
                        severity=sev,
                        category=cat,
                        line=i,
                        match=_redact(line),
                        description=desc,
                    )
                )

        # Invisible / RTL-override unicode => critical obfuscation.
        for char, char_name in INVISIBLE_CHARS.items():
            if char in line:
                findings.append(
                    Finding(
                        pattern_id="obfuscation.invisible_unicode",
                        severity="critical",
                        category="obfuscation",
                        line=i,
                        match=f"U+{ord(char):04X} ({char_name})",
                        description=(
                            f"invisible/bidi unicode {char_name} "
                            "(hidden-text injection vector)"
                        ),
                    )
                )
                break  # one finding per line

        # Long base64 blob => high obfuscation.
        m = _LONG_BASE64_RE.search(line)
        if m and ("obfuscation.long_base64", i) not in seen:
            seen.add(("obfuscation.long_base64", i))
            findings.append(
                Finding(
                    pattern_id="obfuscation.long_base64",
                    severity="high",
                    category="obfuscation",
                    line=i,
                    match=f"<base64 blob, {len(m.group(0))} chars>",
                    description="very long base64 blob (likely encoded payload)",
                )
            )

    return findings


def _verdict_from(findings: list[Finding]) -> str:
    """M2 verdict mapping: critical=>dangerous, high=>caution, else safe."""
    if any(f.severity == "critical" for f in findings):
        return "dangerous"
    if any(f.severity == "high" for f in findings):
        return "caution"
    return "safe"


def _name_from(skill_md_path: Path) -> str:
    """Derive a skill name: parent dir for `.../<name>/SKILL.md`, else stem."""
    if skill_md_path.name.upper() == "SKILL.MD":
        return skill_md_path.parent.name or skill_md_path.stem
    return skill_md_path.stem


def _frontmatter_findings(text: str) -> list[Finding]:
    """Frontmatter structural findings (F3) — never raises.

    - No ``---...---`` block        -> medium ``no parseable frontmatter``.
    - Block present but YAML raises  -> high   ``malformed YAML frontmatter``.
    - Block present, parses to a non-mapping (e.g. a bare scalar/list)
                                     -> high   ``frontmatter is not a mapping``.
    - Block present, parses to a mapping -> no finding.

    A genuinely malformed block is the case the lenient line-by-line parser in
    ``cognition.skills`` silently tolerates; ``yaml.safe_load`` is the oracle
    that distinguishes it from a well-formed one.
    """
    block_match = _FRONTMATTER_BLOCK_RE.match(text)
    if block_match is None:
        return [
            Finding(
                pattern_id="structural.frontmatter_parse",
                severity="medium",
                category="structural",
                line=1,
                match="no frontmatter",
                description="SKILL.md has no parseable YAML frontmatter",
            )
        ]

    block = block_match.group(1)
    try:
        parsed = yaml.safe_load(block)
    except yaml.YAMLError as exc:
        # A real YAML scan/parse error — high (suspicious in a self-authored
        # skill). Strip the verbose location echo to keep the snippet bounded.
        detail = " ".join(str(exc).split())[:_MATCH_MAX_LEN]
        return [
            Finding(
                pattern_id="structural.frontmatter_parse",
                severity="high",
                category="structural",
                line=1,
                match="malformed YAML",
                description=f"malformed YAML frontmatter: {detail}",
            )
        ]
    if not isinstance(parsed, dict):
        return [
            Finding(
                pattern_id="structural.frontmatter_parse",
                severity="high",
                category="structural",
                line=1,
                match=type(parsed).__name__,
                description="frontmatter is not a mapping (expected key/value YAML)",
            )
        ]
    return []


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def scan_skill(skill_md_path: Path) -> ScanResult:
    """Scan a drafted SKILL.md and return a ScanResult.

    Reads the file with ``errors="replace"`` (never raises on bad bytes),
    runs structural checks + a regex rule set + invisible-unicode + long-base64
    detection, and maps findings to a verdict (M2). A read failure, missing
    file, binary content, oversize, symlink, or YAML/parse failure is recorded
    as a *structural Finding* — this function does not raise.
    """
    skill_md_path = Path(skill_md_path)
    name = _name_from(skill_md_path)
    scanned_at = datetime.now(UTC).isoformat()
    findings: list[Finding] = []

    # Read raw bytes for the binary heuristic, then decode replacing bad bytes.
    raw: bytes | None = None
    try:
        raw = skill_md_path.read_bytes()
    except OSError as exc:
        findings.append(
            Finding(
                pattern_id="structural.read_error",
                severity="medium",
                category="structural",
                line=0,
                match=type(exc).__name__,
                description=f"cannot read SKILL.md: {exc}",
            )
        )
        return ScanResult(skill_name=name, verdict=_verdict_from(findings),
                          findings=findings, scanned_at=scanned_at)

    if _looks_binary(raw):
        findings.append(
            Finding(
                pattern_id="structural.binary",
                severity="high",
                category="structural",
                line=0,
                match="binary bytes",
                description="SKILL.md contains binary/non-text bytes",
            )
        )

    text = raw.decode("utf-8", errors="replace")

    findings.extend(_structural_checks(skill_md_path, text))

    # Frontmatter parse check — a parse failure is a structural Finding,
    # NOT an exception.
    #
    # Two distinct cases (F3):
    #   (a) NO frontmatter block at all -> medium "no parseable frontmatter".
    #   (b) A frontmatter block IS present (---...---) but its YAML is
    #       MALFORMED. The lenient line-by-line parser in cognition.skills
    #       happily returns a partial dict for malformed YAML (e.g. an
    #       unterminated quote or a tab-indented mapping), so it CANNOT catch
    #       (b) on its own. We run a REAL ``yaml.safe_load`` over the block; a
    #       raise or a non-mapping result is a high-severity structural finding
    #       (a self-authored skill that the loader can't parse is suspicious,
    #       not merely informational).
    findings.extend(_frontmatter_findings(text))

    findings.extend(_scan_text(text))

    return ScanResult(
        skill_name=name,
        verdict=_verdict_from(findings),
        findings=findings,
        scanned_at=scanned_at,
    )


def sanitize_skill_path_component(component: str) -> str:
    """Slug + validate a model-authored skill ``name``/``category`` (B4).

    HARD-rejects (``ValueError``) anything that could escape ``generated/``:
    path separators (``/`` ``\\``), ``..``, a leading ``.`` (dotfile), an
    absolute path, or an empty/whitespace value. Otherwise lowercases, collapses
    runs of non-``[a-z0-9-]`` (including spaces) to single ``-``, and strips
    leading/trailing ``-``. A traversal token is NOT silently stripped — it is a
    refusal, so ``write_skill`` can never be tricked into writing outside the
    sandbox.

    Examples:
      "valid-name"  -> "valid-name"
      "Data Queries"-> "data-queries"
      ".."          -> ValueError
      "a/b"         -> ValueError
      "a\\b"        -> ValueError
      "/etc/passwd" -> ValueError
      ".hidden"     -> ValueError
    """
    if component is None:
        raise ValueError("skill path component is None")

    raw = component.strip()
    if not raw:
        raise ValueError("empty skill path component")

    # Hard rejects — do not slug these away, refuse outright.
    if raw in (".", ".."):
        raise ValueError(f"unsafe skill path component (dot/dot-dot): {component!r}")
    if ".." in raw:
        raise ValueError(f"unsafe skill path component ('..'): {component!r}")
    if "/" in raw or "\\" in raw:
        raise ValueError(f"unsafe skill path component (separator): {component!r}")
    if raw.startswith("."):
        raise ValueError(f"unsafe skill path component (leading dot): {component!r}")
    if os.path.isabs(raw):
        raise ValueError(f"unsafe skill path component (absolute): {component!r}")
    # Defense in depth: a drive-letter or UNC-ish prefix slipped past isabs.
    if re.match(r"^[A-Za-z]:", raw):
        raise ValueError(f"unsafe skill path component (drive prefix): {component!r}")

    slug = re.sub(r"[^a-z0-9-]+", "-", raw.lower()).strip("-")
    if not slug:
        raise ValueError(f"empty slug from skill path component: {component!r}")
    return slug
