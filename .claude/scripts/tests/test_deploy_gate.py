"""Tests for the P4 deploy gate: deploy-verify.py (the deterministic read-only
seam) and deploy-audit.py (the append-only writer), plus the permanent lock
tests over the deploy workflow and the clients/ compensating control.

Design rules (CLAUDE.md Testing Principle + PRP-client-site-factory-phase-4):
- The base fixture is proven green FIRST (test_pre_verify_green_with_approval)
  so a refusal can only mean the targeted violation was caught — the gate, not
  the fixture, is what refuses.
- One violated dimension per test; every violation test asserts its SPECIFIC
  FAIL-line substring AND the exit code (R2 Major 1: exit 1 alone can hide a
  traceback; deploy-verify maps ANY unexpected exception to exit 2, so a
  1-with-verdict-line is the only shape that counts as a refusal).
- ZERO network (the --post HTTP layer is injected), ZERO vercel invocation,
  ZERO writes outside tmp_path. The repo-level lock tests are read-only.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

_SCRIPTS = Path(__file__).resolve().parents[3] / ".archon" / "scripts"
REPO_ROOT = Path(__file__).resolve().parents[3]


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


dv = _load("deploy_verify", "deploy-verify.py")
da = _load("deploy_audit", "deploy-audit.py")
pc = sys.modules["profile_compile"]  # transitively loaded by deploy-verify

SLUG = "deploy-fixture-client"
BASE = f"/{SLUG}"
CANONICAL = f"https://client.example.test/{SLUG}"
RUN_ID = "run-0001"
FINE_PRINT = "Deploy fixture educational content only."
HOME_WORDS = " ".join(f"alpha{i} bravo{i} charlie{i}" for i in range(20))
GUIDE_WORDS = " ".join(f"delta{i} echo{i} foxtrot{i}" for i in range(20))

def _git(cwd: Path, *args: str) -> None:
    """Throwaway-repo git helper (identity pinned, gpgsign off)."""
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t.test",
         "-c", "commit.gpgsign=false", *args],
        cwd=cwd, capture_output=True, text=True, check=True,
    )


# Mirrors proof-package/YourProduct-client/vercel.json — the exact project-root
# marker resolve_target requires in the deploy target dir.
MARKER_VERCEL_JSON = json.dumps(
    {
        "cleanUrls": True,
        "trailingSlash": False,
        "headers": [
            {
                "source": "/(.*)",
                "headers": [{"key": "X-Robots-Tag", "value": "noindex, nofollow"}],
            }
        ],
    },
    indent=2,
)

CSS_OK = """:root {
  --primary: #111111;
  --primary-2: #222222;
  --ink: #101010;
  --muted: #555555;
  --accent: #cc9900;
  --surface: #fafafa;
  --line: #dddddd;
  --white: #ffffff;
}
body { color: var(--ink); }
"""


def _profile_dict() -> dict:
    font = {"family": "T", "stack": '"T", serif', "file": "fonts/t.woff2"}
    palette = {
        k: "#123456"
        for k in (
            "primary", "primary_2", "ink", "muted", "accent", "accent_deep",
            "accent_soft", "surface", "surface_2", "white", "line", "line_strong",
        )
    }
    return {
        "identity": {
            "slug": SLUG,
            "display_name": "Dana Fixture",
            "org_name": "Deploy Fixture Advisory",
            "vertical": "t",
        },
        "brand": {
            "brand_mark": "D",
            "palette": palette,
            "typography": {"display": dict(font), "body": dict(font), "mono": dict(font)},
            "voice_tone": "calm",
        },
        "facts": {
            "advisor": {"name": "Dana Fixture", "title": "Specialist"},
            "contact": {
                "phone_display": "(555) 010-2020",
                "phone_tel": "+15550102020",
                "email": "dana@deploy-fixture.example",
            },
            "services": [
                {"id": "s1", "name": "Widgets", "short_label": "W", "form_label": "W", "path": "services/w"}
            ],
        },
        "page_plan": {
            "nav": [{"page": "home", "label": "Home"}],
            "nav_cta": {"page": "guide", "label": "Guide"},
            "pages": [
                {
                    "id": "home", "path": "", "template": "home",
                    "meta": {"title": "H", "description": "D", "og_image": "og.png"},
                    "hero": {"poster": "hero.webp"},
                },
                {
                    "id": "guide", "path": "guide", "template": "article",
                    "meta": {"title": "G", "description": "D", "og_image": "og.png"},
                    "hero": {"poster": "hero.webp"},
                },
            ],
        },
        "images": {"persona_pack": "none", "assets_dir": "assets-src"},
        "compliance": {"fine_print": FINE_PRINT},
        "copy_gates": {"min_words": {}, "max_overlap": 0.10},
        "deploy": {
            "held": True,
            "project": "YourProduct-client",
            "base_path": BASE,
            "canonical_base": CANONICAL,
            "meta_robots_noindex": False,
        },
    }


def page_html(body_words: str) -> str:
    return f"""<!doctype html>
<html lang="en">
  <head>
    <title>T</title>
  </head>
  <body>
    <header><nav class="site-nav"><a href="{BASE}">Home</a></nav></header>
    <main>
      <section class="hero"><h1>Heading</h1></section>
      <section class="section"><p>{body_words}</p></section>
    </main>
    <footer>
      <p class="fine-print">{FINE_PRINT}</p>
    </footer>
    <script src="{BASE}/assets/site.js"></script>
  </body>
</html>
"""


@pytest.fixture()
def green(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """A fully green deploy fixture: valid profile + fresh compiled config +
    a build that passes all 13 site-validate checks + a noindex-marked target.
    The approval artifact is NOT stamped here — each test stamps exactly the
    approval it needs (test #1 proves this fixture green WITH a valid one)."""
    monkeypatch.delenv("CLIENT_SITE_DEPLOY_TARGET", raising=False)
    client_dir = tmp_path / "clients" / SLUG
    (client_dir / "compiled").mkdir(parents=True)
    build = client_dir / "build"
    (build / "assets").mkdir(parents=True)
    (build / "guide").mkdir()

    client_yaml = client_dir / "client.yaml"
    client_yaml.write_text(yaml.safe_dump(_profile_dict(), sort_keys=False), encoding="utf-8")
    profile = pc.load_profile(client_yaml)
    (client_dir / "compiled" / "validate.json").write_text(
        json.dumps(pc.build_validate(profile), indent=2), encoding="utf-8"
    )

    (build / "index.html").write_text(page_html(HOME_WORDS), encoding="utf-8")
    (build / "guide" / "index.html").write_text(page_html(GUIDE_WORDS), encoding="utf-8")
    (build / "assets" / "site.css").write_text(CSS_OK, encoding="utf-8")
    (build / "assets" / "site.js").write_text("(function () {})();\n", encoding="utf-8")
    (build / "assets" / "og.png").write_bytes(b"P" * 2048)
    (build / "assets" / "hero.webp").write_bytes(b"W" * 2048)
    (build / "vercel.json").write_text(MARKER_VERCEL_JSON, encoding="utf-8")

    # The target is a git-checkout (live-mode --pre REQUIRES one, R3 Blocker
    # 1) linked to the profile's deploy.project (R3 Major 3), committed clean
    # so the porcelain blast-radius bound starts green.
    target = tmp_path / "YourProduct-client"
    (target / ".vercel").mkdir(parents=True)
    (target / "vercel.json").write_text(MARKER_VERCEL_JSON, encoding="utf-8")
    (target / ".vercel" / "project.json").write_text(
        json.dumps(
            {"projectId": "prj_fixture", "orgId": "team_fixture", "projectName": "YourProduct-client"}
        ),
        encoding="utf-8",
    )
    _git(target, "init", "-q")
    _git(target, "add", "-A")
    _git(target, "commit", "-q", "-m", "baseline")

    return SimpleNamespace(
        client_dir=client_dir,
        client_yaml=client_yaml,
        build=build,
        target=target,
        approval_path=client_dir / "deploy-approval.json",
    )


def stamp_approval(g: SimpleNamespace, **overrides) -> dict:
    """Write a valid live-mode approval bound to the CURRENT physical build.
    Overrides replace single grains; a value of ``...`` (Ellipsis) removes the
    key entirely."""
    approval = {
        "slug": SLUG,
        "approved_at_utc": dv.now_utc().isoformat(),
        "run_id": RUN_ID,
        "acknowledged_hold": True,
        "mode": "live",
        "build_fingerprint": dv.build_fingerprint(g.build),
        "response": "approved",
    }
    approval.update(overrides)
    approval = {k: v for k, v in approval.items() if v is not ...}
    g.approval_path.write_text(json.dumps(approval), encoding="utf-8")
    return approval


def run_pre(g: SimpleNamespace, mode: str = "live", run_id: str | None = RUN_ID,
            target: object = None, extra: list[str] | None = None) -> int:
    argv = [
        "--pre", "--client", str(g.client_yaml), "--mode", mode,
        "--target", str(target if target is not None else g.target),
    ]
    if run_id is not None:
        argv += ["--run-id", run_id]
    if extra:
        argv += extra
    return dv.main(argv)


# ---------------------------------------------------------------------------
# seam: approval existence + basic content
# ---------------------------------------------------------------------------


def test_pre_verify_green_with_approval(green, capsys):
    """#1 — proves the fixture: with a valid live approval, --pre passes."""
    stamp_approval(green)
    rc = run_pre(green)
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "DEPLOY PRE-VERIFY: ok (live)" in out


def test_pre_verify_refuses_without_approval(green, capsys):
    """#2 — THE negative test: no deploy-approval.json -> exit 1 with a
    refusal line that names the approval. Test #1 proves the same fixture
    passes WITH the artifact, so the gate (not the fixture) is what refuses."""
    assert not green.approval_path.exists()
    rc = run_pre(green)
    captured = capsys.readouterr()
    assert rc == 1
    assert "no deploy approval" in captured.out
    assert "Traceback" not in captured.err


def test_pre_verify_refuses_stale_approval(green, capsys):
    """#3 — a 25h-old approval breaches the default 24h max age; the max age
    is resolved at call time (Rule 1), so a wider flag re-admits it."""
    old = (dv.now_utc() - timedelta(hours=25)).isoformat()
    stamp_approval(green, approved_at_utc=old)
    rc = run_pre(green)
    assert rc == 1
    assert "approval stale" in capsys.readouterr().out
    assert run_pre(green, extra=["--approval-max-age-hours", "30"]) == 0


def test_pre_verify_refuses_slug_mismatch(green, capsys):
    stamp_approval(green, slug="other-client")
    rc = run_pre(green)
    assert rc == 1
    assert "approval slug mismatch" in capsys.readouterr().out


def test_pre_verify_refuses_unacknowledged_hold(green, capsys):
    stamp_approval(green, acknowledged_hold=False)
    rc = run_pre(green)
    assert rc == 1
    assert "does not acknowledge the hold" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# seam: the Blocker-2 grain — mode, fingerprint, run
# ---------------------------------------------------------------------------


def test_pre_verify_live_refuses_dry_run_approval(green, capsys):
    """#6 — a dry-run approval NEVER opens a live window (R1 Blocker 2)."""
    stamp_approval(green, mode="dry_run")
    rc = run_pre(green, mode="live")
    assert rc == 1
    assert "does not cover a LIVE deploy" in capsys.readouterr().out


def test_pre_verify_dry_mode_passes_with_dry_run_approval(green, capsys):
    """R2 Minor 2 — one mode vocabulary: a dry_run approval satisfies a
    dry-mode pre-verify, and the frozen-contract alias --mode dry normalizes
    to dry_run instead of failing a generic equality check."""
    stamp_approval(green, mode="dry_run")
    rc = run_pre(green, mode="dry_run")
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "DEPLOY PRE-VERIFY: ok (dry_run)" in out
    assert run_pre(green, mode="dry") == 0


def test_pre_verify_refuses_fingerprint_mismatch(green, capsys):
    """#7 — no replay against a different build: one flipped byte after
    approval refuses. Only the fingerprint dimension is red (the mutation is
    a benign js comment, so site-validate stays green)."""
    stamp_approval(green)
    (green.build / "assets" / "site.js").write_text("(function () {})();/*x*/\n", encoding="utf-8")
    rc = run_pre(green)
    out = capsys.readouterr().out
    assert rc == 1
    assert "build_fingerprint mismatch" in out
    assert "site-validate FAILED" not in out


def test_pre_verify_refuses_run_id_mismatch(green, capsys):
    stamp_approval(green)
    rc = run_pre(green, run_id="other-run")
    assert rc == 1
    assert "approval run_id" in capsys.readouterr().out
    # A caller that states NO run id skips only the run grain (mode +
    # fingerprint + freshness still bind) — the standalone operator-recovery
    # seam the R2 review documents as residual behavior.
    assert run_pre(green, run_id=None) == 0


# ---------------------------------------------------------------------------
# seam: malformed artifacts are refusals, not crashes (R1 Major 3)
# ---------------------------------------------------------------------------


def test_pre_verify_refuses_unparseable_approval(green, capsys):
    green.approval_path.write_bytes(b"{ this is not json")
    rc = run_pre(green)
    captured = capsys.readouterr()
    assert rc == 1
    assert "approval artifact unparseable" in captured.out
    assert "Traceback" not in captured.err


def test_pre_verify_refuses_missing_approved_at(green, capsys):
    """#10 — key absent -> refusal, never a KeyError; a NAIVE timestamp is
    equally invalid (aware-UTC contract, Gotcha #11)."""
    stamp_approval(green, approved_at_utc=...)
    rc = run_pre(green)
    captured = capsys.readouterr()
    assert rc == 1
    assert "approved_at_utc" in captured.out
    assert "Traceback" not in captured.err
    stamp_approval(green, approved_at_utc="2026-07-10T10:00:00")  # naive
    assert run_pre(green) == 1
    assert "approved_at_utc" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# build + validation + derived-config staleness
# ---------------------------------------------------------------------------


def test_pre_verify_refuses_missing_build(green, capsys):
    """#11 — R2 Major 1: the existence check runs BEFORE fingerprinting and a
    missing build is a printed VERDICT at exit 1 — never a traceback exiting 1
    (any unexpected exception exits 2, so rc==1 + the FAIL line is the only
    accepted refusal shape)."""
    stamp_approval(green)
    shutil.rmtree(green.build)
    rc = run_pre(green)
    captured = capsys.readouterr()
    assert rc == 1, "unexpected exceptions must exit 2, refusals must exit 1"
    assert "build/index.html missing" in captured.out
    assert "FAIL  " in captured.out
    assert "Traceback" not in captured.err


def test_pre_verify_refuses_failed_site_validation(green, capsys):
    """#12 — R2 Major 1c: the approval fingerprint is RE-STAMPED after
    poisoning the page, so the in-process site-validate integration is the
    ONLY red dimension this test can pass on."""
    poisoned = page_html(GUIDE_WORDS + " We delve into planning.")
    (green.build / "guide" / "index.html").write_text(poisoned, encoding="utf-8")
    stamp_approval(green)  # binds to the poisoned build
    rc = run_pre(green)
    out = capsys.readouterr().out
    assert rc == 1
    assert "site-validate FAILED" in out
    assert "build_fingerprint mismatch" not in out


def test_pre_verify_refuses_stale_compiled_config(green, capsys):
    """#13 — Rule 2: the compiled view must match the profile. Fine-print
    edited in client.yaml without recompiling -> fine_print_sha256 drift."""
    data = yaml.safe_load(green.client_yaml.read_text(encoding="utf-8"))
    data["compliance"]["fine_print"] = FINE_PRINT + " Amended without recompile."
    green.client_yaml.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    stamp_approval(green)
    rc = run_pre(green)
    assert rc == 1
    assert "stale relative to client.yaml" in capsys.readouterr().out


def test_pre_verify_refuses_stale_gate_config_beyond_fine_print(green, capsys):
    """R3 Major 4 — the Rule 2 partial-proxy: fine-print UNCHANGED, but a
    banned phrase added to client.yaml without recompiling. The single-field
    fine_print_sha256 guard accepted this stale gate config; the whole-view
    comparison against profile_compile.build_validate refuses it."""
    data = yaml.safe_load(green.client_yaml.read_text(encoding="utf-8"))
    data["brand"]["banned_phrases"] = ["unlock your potential"]
    green.client_yaml.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    stamp_approval(green)
    rc = run_pre(green)
    out = capsys.readouterr().out
    assert rc == 1
    assert "stale relative to client.yaml" in out


# ---------------------------------------------------------------------------
# target checks
# ---------------------------------------------------------------------------


def test_pre_verify_refuses_missing_target_dir(green, capsys, tmp_path):
    stamp_approval(green)
    rc = run_pre(green, target=tmp_path / "no-such-target")
    assert rc == 1
    assert "target dir missing" in capsys.readouterr().out


def test_pre_verify_refuses_target_without_noindex_marker(green, capsys):
    stamp_approval(green)
    (green.target / "vercel.json").write_text(json.dumps({"cleanUrls": True}), encoding="utf-8")
    rc = run_pre(green)
    assert rc == 1
    assert "X-Robots-Tag noindex header" in capsys.readouterr().out


def test_pre_verify_refuses_dirty_target_checkout(green, capsys):
    """#16 — blast-radius bound (R1 Major 6), two-sided: dirt OUTSIDE <slug>/
    refuses; dirt ONLY under <slug>/ passes. The fixture's target is already
    a committed-clean throwaway git repo inside tmp_path."""
    stamp_approval(green)
    assert run_pre(green) == 0, capsys.readouterr().out  # clean checkout passes

    (green.target / "junk.txt").write_text("stray file outside the slug", encoding="utf-8")
    rc = run_pre(green)
    assert rc == 1
    assert "changes outside" in capsys.readouterr().out

    (green.target / "junk.txt").unlink()
    slug_dir = green.target / SLUG
    slug_dir.mkdir()
    (slug_dir / "new.html").write_text("dirt under the slug alone is fine", encoding="utf-8")
    assert run_pre(green) == 0, capsys.readouterr().out


# ---------------------------------------------------------------------------
# target identity (R3 Blocker 1): the target must be OUTSIDE this repo
# ---------------------------------------------------------------------------


def test_pre_verify_refuses_in_repo_decoy_target(green, capsys):
    """R3 BLOCKER — proof-package/YourProduct-client (the marker's own donor) is
    the only marker-satisfying dir in every checkout of this repo: dir exists,
    noindex marker present, but it is TRACKED REPO CONTENT with no .git of its
    own, so both blast-radius guards used to vanish on it and a live run would
    rm -rf tracked files. The resolver must refuse it by identity."""
    stamp_approval(green)
    decoy = REPO_ROOT / "proof-package" / "YourProduct-client"
    assert decoy.is_dir(), "the decoy exists in every checkout — the trap is real"
    rc = run_pre(green, target=decoy)
    captured = capsys.readouterr()
    assert rc == 1
    assert "FAIL" in captured.out
    assert "inside this repo" in captured.out
    # the same refusal through --print-target (the seam the bash nodes consume)
    rc = dv.main(["--print-target", "--client", str(green.client_yaml), "--target", str(decoy)])
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert "inside this repo" in captured.err


def test_resolve_target_refuses_worktree_of_this_repo(monkeypatch, tmp_path):
    """R3 Blocker 1 (worktree leg) — a git worktree of this repo lives at an
    arbitrary path OUTSIDE the repo root, so path containment alone misses it;
    the git common dir (physical state, Rule 2) is shared and refuses it. The
    repo under test is a throwaway inside tmp_path, monkeypatched in as 'this
    repo' so the real repo is never mutated."""
    repo_a = tmp_path / "repo-a"
    repo_a.mkdir()
    _git(repo_a, "init", "-q")
    (repo_a / "seed.txt").write_text("x", encoding="utf-8")
    _git(repo_a, "add", "-A")
    _git(repo_a, "commit", "-q", "-m", "seed")
    worktree = tmp_path / "repo-a-worktree"
    _git(repo_a, "worktree", "add", "-q", str(worktree))
    (worktree / "vercel.json").write_text(MARKER_VERCEL_JSON, encoding="utf-8")

    monkeypatch.setattr(dv, "_repo_root", lambda: repo_a.resolve())
    result = dv.resolve_target(str(worktree))
    assert isinstance(result, str), "a worktree of this repo must be a refusal"
    assert "worktree" in result
    # an unrelated external git checkout with the marker still resolves
    external = tmp_path / "external-checkout"
    external.mkdir()
    _git(external, "init", "-q")
    (external / "vercel.json").write_text(MARKER_VERCEL_JSON, encoding="utf-8")
    assert isinstance(dv.resolve_target(str(external)), Path)


def test_pre_verify_live_refuses_non_git_target(green, capsys, tmp_path):
    """R3 Blocker 1 (second half) — on a LIVE run a target WITHOUT .git is an
    EXPLICIT refusal, never a silent guard-skip; dry runs keep accepting
    non-git EXTERNAL targets (the R2-accepted residual)."""
    stamp_approval(green)  # mode live
    bare = tmp_path / "bare-target"
    (bare / ".vercel").mkdir(parents=True)
    (bare / "vercel.json").write_text(MARKER_VERCEL_JSON, encoding="utf-8")
    (bare / ".vercel" / "project.json").write_text(
        json.dumps({"projectId": "p", "orgId": "o", "projectName": "YourProduct-client"}),
        encoding="utf-8",
    )
    rc = run_pre(green, mode="live", target=bare)
    out = capsys.readouterr().out
    assert rc == 1
    assert "cannot verify blast-radius scope: target is not a git checkout" in out
    # dry mode: the same non-git target passes (a live approval covers dry)
    assert run_pre(green, mode="dry_run", target=bare) == 0, capsys.readouterr().out


def test_pre_verify_refuses_unlinked_or_mislinked_vercel_project(green, capsys):
    """R3 Major 3 — deploy.project is a CHECKED grain (Rule 4): a target with
    no .vercel/project.json (a fresh clone; `--yes` would auto-link by
    DIRECTORY NAME) or one linked to a different project refuses BEFORE any
    mutation. Each leg is committed so the porcelain dimension stays green."""
    stamp_approval(green)
    link = green.target / ".vercel" / "project.json"

    link.unlink()
    _git(green.target, "add", "-A")
    _git(green.target, "commit", "-q", "-m", "unlink")
    rc = run_pre(green)
    out = capsys.readouterr().out
    assert rc == 1
    assert "not linked to a deploy project" in out
    assert "changes outside" not in out  # single red dimension

    link.write_text(
        json.dumps({"projectId": "p", "orgId": "o", "projectName": "someone-elses-project"}),
        encoding="utf-8",
    )
    _git(green.target, "add", "-A")
    _git(green.target, "commit", "-q", "-m", "mislink")
    rc = run_pre(green)
    out = capsys.readouterr().out
    assert rc == 1
    assert "deploy.project" in out
    assert "wrong-project deploy refused" in out
    assert "changes outside" not in out


def test_porcelain_rename_into_slug_fails_closed(green, capsys):
    """R3 Minor 5 — a staged `git mv outside.html <slug>/x` produces ONE
    porcelain line `R  outside.html -> <slug>/x` whose text CONTAINS
    ` <slug>/`: the old single-substring filter classified it as in-slug dirt
    and let an OUTSIDE deletion ship. A rename line is in-scope only when
    BOTH halves live under <slug>/."""
    stamp_approval(green)
    (green.target / "outside.html").write_text("victim", encoding="utf-8")
    _git(green.target, "add", "outside.html")
    _git(green.target, "commit", "-q", "-m", "add outside")
    (green.target / SLUG).mkdir()
    _git(green.target, "mv", "outside.html", f"{SLUG}/moved.html")
    rc = run_pre(green)
    assert rc == 1
    assert "changes outside" in capsys.readouterr().out
    # helper matrix: only both-halves-under-slug counts as in-slug
    assert dv._porcelain_line_in_slug(f"R  {SLUG}/a.html -> {SLUG}/b.html", SLUG)
    assert not dv._porcelain_line_in_slug(f"R  outside.html -> {SLUG}/b.html", SLUG)
    assert not dv._porcelain_line_in_slug(f"R  {SLUG}/a.html -> elsewhere/b.html", SLUG)


# ---------------------------------------------------------------------------
# the single resolver + the fingerprint helper
# ---------------------------------------------------------------------------


def test_print_target_flag_beats_env_and_refuses_empty(green, capsys, monkeypatch, tmp_path):
    """#17 — R1 Blocker 1 + Major 4: flag wins over env; empty flag AND
    empty/unset env BOTH refuse, printing NOTHING on stdout."""
    marker_less = tmp_path / "marker-less"
    marker_less.mkdir()
    monkeypatch.setenv("CLIENT_SITE_DEPLOY_TARGET", str(marker_less))
    rc = dv.main(["--print-target", "--client", str(green.client_yaml), "--target", str(green.target)])
    assert rc == 0
    assert capsys.readouterr().out.strip() == green.target.resolve().as_posix()

    monkeypatch.setenv("CLIENT_SITE_DEPLOY_TARGET", "")  # set-but-empty is NOT a target
    rc = dv.main(["--print-target", "--client", str(green.client_yaml), "--target", ""])
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert "REFUSED" in captured.err

    monkeypatch.delenv("CLIENT_SITE_DEPLOY_TARGET", raising=False)
    rc = dv.main(["--print-target", "--client", str(green.client_yaml)])
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""

    monkeypatch.setenv("CLIENT_SITE_DEPLOY_TARGET", str(green.target))
    rc = dv.main(["--print-target", "--client", str(green.client_yaml)])
    assert rc == 0
    assert capsys.readouterr().out.strip() == green.target.resolve().as_posix()


def test_fingerprint_stable_and_sensitive(green, capsys):
    """#18 — same tree twice -> identical; one flipped byte -> different;
    TOTAL on a missing dir (R2 Major 1a: never raises, never matches)."""
    fp1 = dv.build_fingerprint(green.build)
    assert fp1 == dv.build_fingerprint(green.build)
    assert fp1.startswith("sha256:")
    rc = dv.main(["--fingerprint", "--client", str(green.client_yaml)])
    assert rc == 0
    assert capsys.readouterr().out.strip() == fp1
    (green.build / "assets" / "site.js").write_text("(function () {})();/*y*/\n", encoding="utf-8")
    assert dv.build_fingerprint(green.build) != fp1
    missing = dv.build_fingerprint(green.client_dir / "no-such-build")
    assert missing.startswith("sha256:")
    assert missing != fp1


# ---------------------------------------------------------------------------
# --post (HTTP layer injected — no test touches the network)
# ---------------------------------------------------------------------------


def test_post_verify_pass_200_noindex(green, capsys, monkeypatch):
    monkeypatch.setattr(dv, "_default_fetch", lambda url: (200, {"x-robots-tag": "noindex, nofollow"}))
    rc = dv.main(["--post", CANONICAL, "--client", str(green.client_yaml)])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "DEPLOY POST-VERIFY: ok" in out


def test_post_verify_fail_non_200(green, capsys, monkeypatch):
    monkeypatch.setattr(dv, "_default_fetch", lambda url: (404, {"X-Robots-Tag": "noindex"}))
    rc = dv.main(["--post", CANONICAL, "--client", str(green.client_yaml)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "status 404 != 200" in out


def test_post_verify_fail_missing_header(green, capsys, monkeypatch):
    monkeypatch.setattr(dv, "_default_fetch", lambda url: (200, {"Content-Type": "text/html"}))
    rc = dv.main(["--post", CANONICAL, "--client", str(green.client_yaml)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "X-Robots-Tag header missing or lacks noindex" in out


# ---------------------------------------------------------------------------
# audit writer
# ---------------------------------------------------------------------------


def test_audit_appends_jsonl_rows(tmp_path, capsys):
    """#22 — two calls -> two parseable lines, order preserved, append-only."""
    client_dir = tmp_path / SLUG
    client_dir.mkdir()
    assert da.main(["--client-dir", str(client_dir), "--event", "gate_pending",
                    "--verdict", "pending", "--run-id", RUN_ID, "--detail", "gate reached"]) == 0
    assert da.main(["--client-dir", str(client_dir), "--event", "pre_verify",
                    "--verdict", "pass", "--run-id", RUN_ID]) == 0
    lines = (client_dir / "deploy-audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    rows = [json.loads(line) for line in lines]
    assert [r["event"] for r in rows] == ["gate_pending", "pre_verify"]
    assert [r["verdict"] for r in rows] == ["pending", "pass"]
    for row in rows:
        assert row["slug"] == SLUG
        assert row["run_id"] == RUN_ID
        assert row["actor"]
        assert row["ts_utc"]


def test_audit_write_approval_only_on_approval_event(tmp_path):
    """#23 — the artifact writer is event-locked and the grain is mandatory."""
    client_dir = tmp_path / SLUG
    client_dir.mkdir()
    with pytest.raises(SystemExit) as excinfo:
        da.main(["--client-dir", str(client_dir), "--event", "copy", "--verdict", "pass",
                 "--mode", "live", "--build-fingerprint", "sha256:x", "--write-approval"])
    assert excinfo.value.code == 2
    with pytest.raises(SystemExit):  # grain mandatory: no mode / no fingerprint
        da.main(["--client-dir", str(client_dir), "--event", "approval",
                 "--verdict", "approved", "--write-approval"])
    assert not (client_dir / "deploy-approval.json").exists()

    rc = da.main(["--client-dir", str(client_dir), "--event", "approval", "--verdict", "approved",
                  "--run-id", RUN_ID, "--mode", "live", "--build-fingerprint", "sha256:abc",
                  "--response", "approved", "--write-approval"])
    assert rc == 0
    artifact = json.loads((client_dir / "deploy-approval.json").read_text(encoding="utf-8"))
    assert artifact["slug"] == SLUG
    assert artifact["mode"] == "live"
    assert artifact["build_fingerprint"] == "sha256:abc"
    assert artifact["acknowledged_hold"] is True
    assert artifact["run_id"] == RUN_ID
    assert artifact["approved_at_utc"]


def test_audit_consume_approval_renames_artifact(tmp_path):
    """#24 — consume is close-event-only; the artifact is renamed, never
    deleted (one approval = at most one real attempt)."""
    client_dir = tmp_path / SLUG
    client_dir.mkdir()
    da.main(["--client-dir", str(client_dir), "--event", "approval", "--verdict", "approved",
             "--mode", "live", "--build-fingerprint", "sha256:abc", "--write-approval"])
    with pytest.raises(SystemExit):
        da.main(["--client-dir", str(client_dir), "--event", "copy",
                 "--verdict", "pass", "--consume-approval"])
    assert (client_dir / "deploy-approval.json").exists()  # wrong event consumed nothing

    rc = da.main(["--client-dir", str(client_dir), "--event", "close",
                  "--verdict", "pass", "--consume-approval"])
    assert rc == 0
    assert not (client_dir / "deploy-approval.json").exists()
    consumed = list(client_dir.glob("deploy-approval.consumed-*.json"))
    assert len(consumed) == 1
    rows = [json.loads(l) for l in (client_dir / "deploy-audit.jsonl").read_text(encoding="utf-8").splitlines()]
    assert "approval consumed" in rows[-1]["detail"]


def test_audit_consume_missing_artifact_is_noop(tmp_path):
    """R2 Minor 5c — close with nothing to consume is a NOTED no-op, not an
    error (a dry run's close path must never fail on the absent artifact)."""
    client_dir = tmp_path / SLUG
    client_dir.mkdir()
    rc = da.main(["--client-dir", str(client_dir), "--event", "close",
                  "--verdict", "dry_run", "--consume-approval"])
    assert rc == 0
    rows = [json.loads(l) for l in (client_dir / "deploy-audit.jsonl").read_text(encoding="utf-8").splitlines()]
    assert "no approval artifact to consume" in rows[-1]["detail"]


# ---------------------------------------------------------------------------
# exit-2 infra contract (R1 Major 3 / R2 Major 1b)
# ---------------------------------------------------------------------------


def test_verify_infra_exit_2_on_invalid_profile(tmp_path, capsys):
    """#25 — a broken profile is INFRA (exit 2, stderr), never a verdict."""
    client_dir = tmp_path / SLUG
    client_dir.mkdir()
    broken = client_dir / "client.yaml"
    broken.write_text("identity: {}\n", encoding="utf-8")
    rc = dv.main(["--pre", "--client", str(broken), "--mode", "live"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "INFRA ERROR" in captured.err
    assert "FAIL" not in captured.out
    # missing client.yaml entirely is also infra, for every mode
    rc = dv.main(["--fingerprint", "--client", str(tmp_path / "nope" / "client.yaml")])
    captured = capsys.readouterr()
    assert rc == 2
    assert "INFRA ERROR" in captured.err


def test_audit_infra_exit_2_missing_client_dir(tmp_path, capsys):
    """#26 — exit 2 AND the jsonl is never created (no partial write)."""
    missing = tmp_path / "no-such-client"
    rc = da.main(["--client-dir", str(missing), "--event", "close", "--verdict", "fail"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "INFRA ERROR" in captured.err
    assert not (missing / "deploy-audit.jsonl").exists()


# ---------------------------------------------------------------------------
# compensating control + permanent locks (R1 Major 5)
# ---------------------------------------------------------------------------


def test_clients_dir_untracked_and_ignored():
    """The WS3-deferred sanitizer deny entry's compensating control: clients/
    is gitignored and scripts/sanitize.py enumerates via `git ls-files`, so
    untracked files can never ship publicly.

    SUCCESSOR (R1 Minor 7): this test goes red BY DESIGN when WS3 flips
    clients/ to tracked+sanitizer-denied. When that lands, REPLACE this test
    with the WS3 sanitizer-deny leak test (plant a clients/ file + the private
    manual chapter, run the export, prove neither reaches the public tree) —
    upgrade the control, don't delete the red test."""
    ls = subprocess.run(
        ["git", "ls-files", "clients/"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    )
    assert ls.stdout.strip() == "", f"clients/ must stay untracked, found: {ls.stdout[:200]}"
    probe = subprocess.run(
        ["git", "check-ignore", "clients/__probe__"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    assert probe.returncode == 0, "clients/ must be gitignored (.gitignore clients/ entry)"


def test_no_workflow_references_deploy_workflow():
    """#28 (scope extended per R2 Minor 3) — the factory, any other workflow,
    any command prompt, and any .archon script can NEVER trigger the deploy
    DAG: the literal workflow name appears nowhere but in the deploy workflow
    itself. Operator invocation is the only entry point."""
    needle = "client-site-deploy"
    scanned = [
        p for p in sorted((REPO_ROOT / ".archon" / "workflows").glob("*.yaml"))
        if p.name != "client-site-deploy.yaml"
    ]
    scanned += sorted((REPO_ROOT / ".archon" / "commands").glob("*.md"))
    scanned += sorted((REPO_ROOT / ".archon" / "scripts").glob("*.py"))
    offenders = [
        str(p) for p in scanned
        if needle in p.read_text(encoding="utf-8", errors="replace")
    ]
    assert offenders == [], f"deploy workflow referenced outside itself: {offenders}"


def test_deploy_dag_gating_edges_locked():
    """#29 — parsed-YAML lock (not grep): the three mutating nodes are
    when-guarded on dry_run == 'false' and their depends chain reaches BOTH
    pre-verify and the deploy-gate itself (R2 Minor 4: re-parenting around
    the gate must turn this red). The gate carries NO on_reject (Gotcha #1:
    reject must CANCEL the run, not rework it)."""
    path = REPO_ROOT / ".archon" / "workflows" / "client-site-deploy.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    nodes = {node["id"]: node for node in data["nodes"]}

    for node_id in ("copy-build", "vercel-deploy", "post-verify"):
        assert "dry_run == 'false'" in nodes[node_id].get("when", ""), (
            f"{node_id} lost its dry_run guard"
        )

    def reaches(start: str, goal: str) -> bool:
        seen: set[str] = set()
        stack = [start]
        while stack:
            current = stack.pop()
            if current == goal:
                return True
            if current in seen:
                continue
            seen.add(current)
            stack.extend(nodes.get(current, {}).get("depends_on", []))
        return False

    for node_id in ("copy-build", "vercel-deploy", "post-verify"):
        assert reaches(node_id, "pre-verify"), f"{node_id} does not chain through pre-verify"
        assert reaches(node_id, "deploy-gate"), f"{node_id} does not chain through deploy-gate"

    gate = nodes["deploy-gate"]["approval"]
    assert "on_reject" not in gate, "the deploy gate must NOT rework on reject — reject cancels"


def _deploy_yaml_nodes() -> dict:
    path = REPO_ROOT / ".archon" / "workflows" / "client-site-deploy.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return {node["id"]: node for node in data["nodes"]}


def test_preflight_resolves_target_before_gate():
    """R3 Major 2 — the operator must approve a VISIBLE aim point: preflight
    resolves the target through THE resolver (--print-target) and the DEPLOY
    PREVIEW line the gate message interpolates carries it. The resolution
    happens BEFORE the gate_pending row is written — a resolver refusal fails
    preflight and the run stops before the gate (gate_pending means the gate
    was reached)."""
    bash = _deploy_yaml_nodes()["preflight"]["bash"]
    resolve_at = bash.find("--print-target")
    gate_row_at = bash.find("gate_pending")
    assert resolve_at != -1, "preflight must resolve the deploy target"
    assert gate_row_at != -1
    assert resolve_at < gate_row_at, "the target must resolve BEFORE the gate_pending row"
    preview = next(line for line in bash.splitlines() if "DEPLOY PREVIEW" in line)
    assert "target=$TARGET" in preview, "the gate preview must show the resolved target"


def test_vercel_deploy_guard_rename_aware_and_git_mandatory():
    """R3 Blocker 1 (deploy-time half) + Minor 5 — the porcelain guard can
    never vanish silently (a non-git target is a hard REFUSE at mutation
    time) and the noise filter is rename-aware (the rename-blind
    single-substring grep is gone)."""
    bash = _deploy_yaml_nodes()["vercel-deploy"]["bash"]
    assert '[ -d "$TARGET/.git" ] ||' in bash, "non-git target must hard-fail, not skip"
    assert "not a git checkout" in bash
    assert 'grep -v " ${SLUG}/"' not in bash, "the rename-blind filter must stay dead"
    assert " -> " in bash, "the filter must special-case rename porcelain lines"


def test_audit_close_verdict_scoped_to_run_rows(tmp_path):
    """R3 Minor 6 — the close verdict comes from THIS run's rows only:
    another actor's later row and a corrupt trailing half-line (crash
    mid-append) can neither flip nor crash the inference, and zero matching
    rows falls back to fail. Executes the ACTUAL python program embedded in
    the audit-close node."""
    bash = _deploy_yaml_nodes()["audit-close"]["bash"]
    match = re.search(r'python -c "\n(.+?)\n"', bash, re.DOTALL)
    assert match, "audit-close must carry the run-scoped verdict program"
    program = match.group(1)
    assert "RUN_ID" in program and "run_id" in program

    client_dir = tmp_path / SLUG
    client_dir.mkdir()
    rows = [
        {"run_id": "run-A", "event": "pre_verify", "verdict": "pass"},
        {"run_id": "run-A", "event": "post_verify", "verdict": "pass"},
        {"run_id": "run-B", "event": "post_verify", "verdict": "fail"},  # another actor, later
    ]
    payload = "\n".join(json.dumps(r) for r in rows) + "\n" + '{"corrupt": '
    (client_dir / "deploy-audit.jsonl").write_text(payload, encoding="utf-8")

    def infer(run_id: str) -> str:
        env = dict(os.environ, CLIENT_DIR=str(client_dir), RUN_ID=run_id)
        proc = subprocess.run(
            [sys.executable, "-c", program], capture_output=True, text=True, env=env
        )
        assert proc.returncode == 0, proc.stderr
        return proc.stdout.strip()

    assert infer("run-A") == "post_verify/pass"  # not run-B's trailing fail
    assert infer("run-B") == "post_verify/fail"
    assert infer("run-C") == "none/fail"  # zero rows for this run -> fail


def test_no_shell_vars_inside_python_c_source():
    """R3 Minor 7 — no shell variable is ever interpolated into python -c
    SOURCE (quote-fragile and injection-shaped); paths travel via env and are
    read with os.environ inside the program."""
    path = REPO_ROOT / ".archon" / "workflows" / "client-site-deploy.yaml"
    text = path.read_text(encoding="utf-8")
    programs = re.findall(r'python -c "([^"]*)"', text)
    assert programs, "expected python -c programs in the deploy workflow"
    offenders = [p for p in programs if "$" in p]
    assert offenders == [], f"shell interpolation inside python source: {offenders}"


def _non_docstring_strings(tree: ast.AST) -> list[str]:
    doc_ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                doc_ids.add(id(body[0].value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and id(node) not in doc_ids
    ]


def test_no_vercel_cli_in_python_scripts():
    """#30 — the deploy CLI stays out of pytest-reachable code, permanently.

    DEVIATION FROM THE PRP's raw-text grep (documented): the raw scan goes red
    on PRE-EXISTING check-8 marker code — site-validate.py's local variable
    named `vercel`, site-assembler.py's VERCEL_JSON constant, and the module
    docstring phrase 'Vercel project root'. None of those can invoke anything.
    The lock's intent is that no .archon script can ever RUN the CLI, and an
    invocation requires the token inside a runtime STRING (subprocess argv,
    os.system, shell fragment). So: scan every non-docstring string constant
    (f-string parts included), strip the two filesystem-path carve-outs —
    'vercel.json' (the noindex marker filename) and '.vercel' (the standard
    link directory deploy-verify's R3-Major-3 project-grain check reads; a
    dot-prefixed dirname is not an invokable token, and the scan stays
    case-insensitive because Windows PATH lookup is) — and refuse any
    surviving 'vercel'."""
    offenders: list[str] = []
    for script in sorted((REPO_ROOT / ".archon" / "scripts").glob("*.py")):
        tree = ast.parse(script.read_text(encoding="utf-8"), filename=str(script))
        for value in _non_docstring_strings(tree):
            cleaned = re.sub(r"vercel\.json|\.vercel", "", value, flags=re.IGNORECASE)
            if "vercel" in cleaned.lower():
                offenders.append(f"{script.name}: {value!r}")
    assert offenders == [], f"CLI-capable 'vercel' string in a python script: {offenders}"
