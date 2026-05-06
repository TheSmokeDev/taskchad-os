"""PRP-7e Phase 5 / WS3 — real-Archon-binary profile isolation acceptance gate.

This is the PRD §16 row 6 acceptance gate. It exercises the **real** ``archon``
binary end-to-end on TWO profiles (sales + engineering), sequential AND
concurrent, and asserts on physical disk state under each
``<profile>/.archon/`` root. NO mocking the binary. If ``archon`` is not on
PATH, the whole module is skipped — but R1 B3 requires the binary to be
present in CI / local dev where the gate is meaningful.

What this proves
----------------

* `ARCHON_HOME=<profile>/.archon` redirects state + workflow discovery into
  the per-profile dir (R4 architectural pivot — supersedes R3 Option C
  ``--cwd $HOMIE_HOME``). ``archon workflow list --json`` lists the seeded
  ``profile-isolation-smoke`` workflow because it lives at
  ``<ARCHON_HOME>/workflows/profile-isolation-smoke.yaml``.
* The smoke workflow's marker + ralph-state nodes write to
  ``$HOMIE_HOME/.archon/artifacts/profile-marker.txt`` and
  ``$HOMIE_HOME/.archon/ralph/profile-isolation-smoke/state.txt`` using
  ``${HOMIE_NAME}`` strict expansion. Two profiles → two distinct content
  payloads under two distinct disk roots.
* ``personas.get_subprocess_env(extra_env)`` is the single supported
  contract for building the subprocess env — it wires HOME / USERPROFILE
  per profile (R3 NM2) and merges ``extra_env`` last so the wrapper's
  ``ARCHON_HOME`` / ``HOMIE_HOME`` / ``HOMIE_NAME`` overrides win.
* ``ProcessPoolExecutor`` (NOT thread) is the correct concurrency primitive
  because subprocess env isolation needs distinct OS processes — threads
  share ``os.environ`` and would race on the per-test setenv.

What this does NOT cover
------------------------

Unit-level cmd-shape regression (Pattern A monkeypatched ``subprocess.run``)
is in ``test_persona_archon_runner.py`` (WS2b). This module owns the live
binary contract; the unit tests own the call-shape contract. Together
they form the complete R3 NB1 / NM1 acceptance surface.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import pytest


# =============================================================================
# MODULE-LEVEL SKIP (R1 B3 — real binary or skip, never mock)
# =============================================================================

pytestmark = pytest.mark.skipif(
    shutil.which("archon") is None,
    reason="archon binary not on PATH — install via "
    "'curl -fsSL https://archon.thehomie.ai/install.sh | bash'. "
    "This module is the PRD §16 row 6 acceptance gate; it requires the "
    "real binary to validate live isolation contracts.",
)


# =============================================================================
# FIXTURES
# =============================================================================


@pytest.fixture
def tmp_archon_repo(tmp_path: Path) -> Path:
    """Create a minimal real git repo for ``archon``'s git-probe.

    Archon's workflow runner refuses to run inside a non-git ``--cwd`` and
    R3 NB1 caught a class of bugs where ``--cwd <profile_root>`` was
    passed (non-git) instead of ``--cwd <git_repo>``. The fixture creates
    a TINY repo (one committed file) — NOT a clone of thehomie — so
    each test gets its own throw-away cwd that satisfies the git probe.

    Returns the absolute Path to the repo root.
    """
    repo = tmp_path / "archon-tmp-repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init"], cwd=repo, check=True, capture_output=True
    )
    (repo / "README.md").write_text("tmp\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "."], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t.t",
            "commit",
            "-m",
            "init",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo


@pytest.fixture
def two_profile_archon_setup(
    multi_profile_fixture: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Path]:
    """Run ``init_archon`` on BOTH sales + engineering profiles.

    Builds on ``multi_profile_fixture`` (which seeds the empty profile
    directory inventory + a ``home/`` dir for ``get_subprocess_env`` to
    pick up). Then invokes the real ``personas.archon.init_archon`` on
    each profile so that:

    * ``<profile>/.archon/config.yaml`` is written (PRD §11.1 shape)
    * ``<profile>/.archon/workflows/profile-isolation-smoke.yaml`` is seeded
    * the directory inventory (workflows / commands / artifacts / ralph /
      worktrees) is fully scaffolded

    Returns the same dict as ``multi_profile_fixture``::

        {"sales": <profile_root>, "engineering": <profile_root>}

    NOTE: the test-process os.environ for HOMIE_HOME / HOMIE_NAME is left
    pointing at whatever ``multi_profile_fixture`` set (the empty homie
    root). Each test flips it to the target profile before invoking the
    archon binary.
    """
    from personas.archon import init_archon

    for name in ("sales", "engineering"):
        # init_archon resolves the archon root via get_persona_paths(name)
        # — it does NOT need HOMIE_HOME set on the test process.
        archon_root = init_archon(name)
        # Sanity check — fixture must yield a real .archon dir + smoke YAML.
        assert archon_root == multi_profile_fixture[name] / ".archon"
        assert archon_root.is_dir(), f"init_archon failed to create {archon_root}"
        assert (archon_root / "config.yaml").is_file()
        assert (
            archon_root / "workflows" / "profile-isolation-smoke.yaml"
        ).is_file(), (
            "smoke YAML not seeded — verify "
            ".claude/templates/profile-isolation-smoke.yaml exists"
        )
    return multi_profile_fixture


# =============================================================================
# HELPERS
# =============================================================================


def _strip_archon_log_lines(stdout: str) -> str:
    """Remove archon's pino JSON-line log preamble from stdout.

    ``archon workflow list --json`` mixes JSON-line workflow.discovery logs
    with the actual ``{"workflows": [...]}`` JSON object on stdout. Even
    with ``--quiet`` the ``[archon] loaded ...`` preamble can still show
    up depending on plugin state. Strip everything that is NOT part of the
    final JSON object so ``json.loads`` succeeds.

    Strategy: scan for the FIRST line that starts with a bare ``{`` AND
    is followed by a line beginning with whitespace + ``"workflows"``.
    Return the slice from that line onward.
    """
    lines = stdout.splitlines()
    for idx, line in enumerate(lines):
        # The actual JSON object always opens with a bare ``{`` on its own
        # line (yaml-pretty-printed). Pino log lines are minified JSON on
        # one line each, so they never break across newlines.
        if line.strip() == "{" and idx + 1 < len(lines):
            nxt = lines[idx + 1].lstrip()
            if nxt.startswith('"workflows"'):
                return "\n".join(lines[idx:])
    # Fall back to the original — caller's json.loads will surface the
    # error with full context.
    return stdout


def _read_marker(profile_root: Path) -> str:
    """Return the contents of ``<profile>/.archon/artifacts/profile-marker.txt``."""
    marker = profile_root / ".archon" / "artifacts" / "profile-marker.txt"
    assert marker.is_file(), (
        f"profile-marker.txt missing at {marker}. Smoke workflow did not "
        f"reach the write-marker node — check archon stderr."
    )
    return marker.read_text(encoding="utf-8")


def _read_ralph_state(profile_root: Path) -> dict:
    """Return the parsed JSON dict from ``<profile>/.archon/ralph/.../state.txt``.

    PRP-7e WS2a decision: file extension is ``.txt`` but content is JSON.
    """
    state_file = (
        profile_root
        / ".archon"
        / "ralph"
        / "profile-isolation-smoke"
        / "state.txt"
    )
    assert state_file.is_file(), (
        f"ralph state.txt missing at {state_file}. Smoke workflow did not "
        f"reach the write-ralph-state node."
    )
    return json.loads(state_file.read_text(encoding="utf-8"))


# =============================================================================
# TEST 1 — REAL DISCOVERY VIA ARCHON_HOME
# =============================================================================


def test_smoke_workflow_real_discovery(
    two_profile_archon_setup: dict[str, Path],
    tmp_archon_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``archon workflow list --json`` discovers the seeded profile-isolation-smoke.

    Proves the R4 architectural pivot: per-profile state isolation lives
    at ``<profile>/.archon/`` via the ``ARCHON_HOME`` env var, NOT via
    ``--cwd $HOMIE_HOME``. Archon's discovery layering picks up the
    ``profile-isolation-smoke.yaml`` from ``<ARCHON_HOME>/workflows/``
    in addition to its bundled defaults.

    R3 NM1 unit-level companion at WS2b is monkeypatched; THIS is the
    real-binary discovery proof.
    """
    sales_root = two_profile_archon_setup["sales"]
    archon_home = sales_root / ".archon"

    # Seed os.environ so personas.get_subprocess_env picks up the per-profile
    # home/ dir for HOME/USERPROFILE rewriting (R3 NM2 contract).
    monkeypatch.setenv("HOMIE_HOME", str(sales_root))
    monkeypatch.setenv("HOMIE_NAME", "sales")

    from personas import get_subprocess_env

    env = get_subprocess_env(
        {
            "ARCHON_HOME": str(archon_home),
            "HOMIE_HOME": str(sales_root),
            "HOMIE_NAME": "sales",
            "ARCHON_SUPPRESS_NESTED_CLAUDE_WARNING": "1",
        }
    )

    # Pre-assert env shape BEFORE subprocess.run — proves get_subprocess_env
    # honored every contract knob this acceptance gate cares about.
    assert env["HOMIE_HOME"] == str(sales_root)
    assert env["HOMIE_NAME"] == "sales"
    assert env["ARCHON_HOME"] == str(archon_home)
    # HOME (POSIX + Windows fallback) — get_subprocess_env sets it to
    # <HOMIE_HOME>/home when that dir exists. multi_profile_fixture seeds it.
    expected_home = sales_root / "home"
    assert env["HOME"] == str(expected_home), (
        f"get_subprocess_env did not rewrite HOME to {expected_home}; "
        f"got env['HOME']={env.get('HOME')!r}. Verify multi_profile_fixture "
        f"seeded {expected_home}."
    )
    if sys.platform == "win32":
        assert env["USERPROFILE"] == str(expected_home), (
            f"USERPROFILE not rewritten on win32 — got "
            f"{env.get('USERPROFILE')!r}, expected {expected_home}"
        )

    # Invoke real archon. ``--quiet`` suppresses pino discovery logs so
    # stdout is clean JSON — fall back to _strip_archon_log_lines if any
    # other preamble sneaks in.
    cmd = [
        "archon",
        "workflow",
        "list",
        "--json",
        "--quiet",
        "--cwd",
        str(tmp_archon_repo),
    ]
    result = subprocess.run(
        cmd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"archon workflow list failed: returncode={result.returncode}\n"
        f"stdout (first 400): {result.stdout[:400]!r}\n"
        f"stderr (first 400): {result.stderr[:400]!r}"
    )
    cleaned = _strip_archon_log_lines(result.stdout)
    payload = json.loads(cleaned)
    workflow_names = [w["name"] for w in payload.get("workflows", [])]
    assert "profile-isolation-smoke" in workflow_names, (
        f"profile-isolation-smoke not discovered. Got workflows: "
        f"{workflow_names!r}. Verify ARCHON_HOME points at "
        f"{archon_home} and that workflows/profile-isolation-smoke.yaml "
        f"is present there."
    )


# =============================================================================
# TEST 2 — SEQUENTIAL TWO-PROFILE ACCEPTANCE
# =============================================================================


def _run_smoke_for_profile(
    profile_root: Path,
    name: str,
    tmp_archon_repo: Path,
) -> subprocess.CompletedProcess:
    """Invoke ``archon workflow run profile-isolation-smoke`` for one profile.

    Used by the sequential test (called inline). The concurrent test calls
    a separate worker function defined at module top level.

    The function sets ``os.environ["HOMIE_HOME"]`` + ``["HOMIE_NAME"]``
    BEFORE calling ``personas.get_subprocess_env`` so the helper resolves
    the right per-profile ``home/`` dir for HOME/USERPROFILE rewriting
    (R3 NM2 fix verified by WS3).
    """
    archon_home = profile_root / ".archon"

    os.environ["HOMIE_HOME"] = str(profile_root)
    os.environ["HOMIE_NAME"] = name

    from personas import get_subprocess_env

    env = get_subprocess_env(
        {
            "ARCHON_HOME": str(archon_home),
            "HOMIE_HOME": str(profile_root),
            "HOMIE_NAME": name,
            "ARCHON_SUPPRESS_NESTED_CLAUDE_WARNING": "1",
        }
    )

    # Pre-assert shape — every value the smoke workflow + R4 isolation
    # contract depends on.
    assert env["HOMIE_HOME"] == str(profile_root)
    assert env["HOMIE_NAME"] == name
    assert env["ARCHON_HOME"] == str(archon_home)
    if sys.platform == "win32":
        assert env["USERPROFILE"] == str(profile_root / "home")
    else:
        assert env["HOME"] == str(profile_root / "home")

    cmd = [
        "archon",
        "workflow",
        "run",
        "profile-isolation-smoke",
        "--quiet",
        "--no-worktree",
        "--cwd",
        str(tmp_archon_repo),
    ]
    return subprocess.run(
        cmd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )


def test_two_profiles_distinct_state_sequential(
    two_profile_archon_setup: dict[str, Path],
    tmp_archon_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sales and Engineering profiles produce distinct on-disk state.

    Sequential run — simpler diagnostic that exercises the env-isolation
    contract without race conditions. Asserts:

    * config.yaml under each profile is a distinct file
    * profile-marker.txt content keys on the right HOMIE_NAME
    * ralph state.txt JSON.profile keys on the right HOMIE_NAME
    * smoke YAML is present under both profiles (init_archon seeded both)
    * NO orphan worktrees / branches outside the per-profile root
    """
    sales_root = two_profile_archon_setup["sales"]
    eng_root = two_profile_archon_setup["engineering"]

    # Restore os.environ on test exit (the helper mutates it).
    monkeypatch.setenv("HOMIE_HOME", str(sales_root))
    monkeypatch.setenv("HOMIE_NAME", "sales")

    # Run sales first.
    result_sales = _run_smoke_for_profile(sales_root, "sales", tmp_archon_repo)
    assert result_sales.returncode == 0, (
        f"sales smoke run failed: returncode={result_sales.returncode}\n"
        f"stdout (first 800): {result_sales.stdout[:800]!r}\n"
        f"stderr (first 800): {result_sales.stderr[:800]!r}"
    )

    # Run engineering second.
    result_eng = _run_smoke_for_profile(eng_root, "engineering", tmp_archon_repo)
    assert result_eng.returncode == 0, (
        f"engineering smoke run failed: returncode={result_eng.returncode}\n"
        f"stdout (first 800): {result_eng.stdout[:800]!r}\n"
        f"stderr (first 800): {result_eng.stderr[:800]!r}"
    )

    # ---- Distinct config.yaml files (different physical paths) ----
    sales_cfg = sales_root / ".archon" / "config.yaml"
    eng_cfg = eng_root / ".archon" / "config.yaml"
    assert sales_cfg.is_file()
    assert eng_cfg.is_file()
    assert sales_cfg != eng_cfg
    assert sales_cfg.parent != eng_cfg.parent

    # ---- profile-marker.txt content keys on the right name ----
    sales_marker = _read_marker(sales_root)
    eng_marker = _read_marker(eng_root)
    assert "profile=sales" in sales_marker, (
        f"sales marker missing 'profile=sales' line. Content: {sales_marker!r}"
    )
    assert "profile=engineering" in eng_marker, (
        f"engineering marker missing 'profile=engineering' line. "
        f"Content: {eng_marker!r}"
    )
    # Cross-contamination check — engineering's name must NOT show in sales
    # marker, and vice-versa.
    assert "profile=engineering" not in sales_marker
    assert "profile=sales" not in eng_marker

    # ---- ralph state.txt JSON.profile keys on the right name (R1 M6) ----
    sales_state = _read_ralph_state(sales_root)
    eng_state = _read_ralph_state(eng_root)
    assert sales_state["profile"] == "sales"
    assert eng_state["profile"] == "engineering"

    # ---- Smoke YAML present under BOTH profiles (init_archon seeded both) ----
    assert (
        sales_root / ".archon" / "workflows" / "profile-isolation-smoke.yaml"
    ).is_file()
    assert (
        eng_root / ".archon" / "workflows" / "profile-isolation-smoke.yaml"
    ).is_file()

    # ---- No orphan worktrees outside the per-profile worktrees/ dir ----
    # ``--no-worktree`` prevents archon from creating any worktree at all,
    # so the worktrees/ dir under each profile must remain EMPTY (or
    # contain only metadata the init scaffold seeded — which is none).
    sales_worktrees = sales_root / ".archon" / "worktrees"
    eng_worktrees = eng_root / ".archon" / "worktrees"
    assert sales_worktrees.is_dir()
    assert eng_worktrees.is_dir()
    sales_wt_children = list(sales_worktrees.iterdir())
    eng_wt_children = list(eng_worktrees.iterdir())
    assert sales_wt_children == [], (
        f"sales worktrees/ leaked children: {sales_wt_children!r}"
    )
    assert eng_wt_children == [], (
        f"engineering worktrees/ leaked children: {eng_wt_children!r}"
    )

    # ---- No orphan branches in the tmp_archon_repo (the cwd) ----
    # archon --no-worktree could in principle create branches. Verify the
    # cwd repo's branch list still only contains the initial commit's
    # default branch.
    branches_result = subprocess.run(
        ["git", "branch", "--list"],
        cwd=tmp_archon_repo,
        capture_output=True,
        text=True,
        check=True,
    )
    branch_lines = [
        line.strip().lstrip("* ").strip()
        for line in branches_result.stdout.splitlines()
        if line.strip()
    ]
    # Default branch is whatever ``git init`` configured (master / main).
    # We assert there is exactly ONE branch — no archon-created leftovers.
    assert len(branch_lines) <= 1, (
        f"orphan branches in tmp_archon_repo after sequential run: "
        f"{branch_lines!r}"
    )


# =============================================================================
# TEST 3 — CONCURRENT TWO-PROFILE ACCEPTANCE (R1 M3 — the gate)
# =============================================================================


def _concurrent_worker(
    profile_root_str: str, name: str, tmp_repo_str: str, scripts_dir_str: str
) -> dict:
    """ProcessPoolExecutor worker — DEFINED AT MODULE TOP-LEVEL FOR SERIALIZATION.

    Each worker:

    1. Inserts the thehomie ``.claude/scripts/`` dir into ``sys.path``
       so ``personas`` and submodules are importable in the fresh process
       (the parent's ``conftest.py`` does this automatically; the worker
       gets a brand-new interpreter and must repeat the setup).
    2. Sets ``os.environ["HOMIE_HOME"]`` + ``["HOMIE_NAME"]`` so the
       helper picks up the right profile.
    3. Builds env via ``personas.get_subprocess_env`` and asserts the
       contract values (HOMIE_HOME / HOMIE_NAME / ARCHON_HOME / HOME /
       USERPROFILE).
    4. Invokes ``archon workflow run profile-isolation-smoke`` and
       returns the result as a plain dict so it round-trips cleanly
       across the process boundary on Windows.
    """
    # Step 1 — sys.path setup so ``from personas import ...`` works in the
    # fresh worker process.
    if scripts_dir_str not in sys.path:
        sys.path.insert(0, scripts_dir_str)

    profile_root = Path(profile_root_str)
    tmp_archon_repo = Path(tmp_repo_str)
    archon_home = profile_root / ".archon"

    # Step 2 — set per-process os.environ (each worker is its own OS
    # process, so this does NOT race with sibling workers).
    os.environ["HOMIE_HOME"] = str(profile_root)
    os.environ["HOMIE_NAME"] = name

    # Step 3 — build subprocess env via personas helper + pre-assert.
    from personas import get_subprocess_env  # imported in worker process

    env = get_subprocess_env(
        {
            "ARCHON_HOME": str(archon_home),
            "HOMIE_HOME": str(profile_root),
            "HOMIE_NAME": name,
            "ARCHON_SUPPRESS_NESTED_CLAUDE_WARNING": "1",
        }
    )
    assert env["HOMIE_HOME"] == str(profile_root)
    assert env["HOMIE_NAME"] == name
    assert env["ARCHON_HOME"] == str(archon_home)
    if sys.platform == "win32":
        assert env["USERPROFILE"] == str(profile_root / "home")
    else:
        assert env["HOME"] == str(profile_root / "home")

    # Step 4 — invoke real archon.
    cmd = [
        "archon",
        "workflow",
        "run",
        "profile-isolation-smoke",
        "--quiet",
        "--no-worktree",
        "--cwd",
        str(tmp_archon_repo),
    ]
    completed = subprocess.run(
        cmd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    return {
        "name": name,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def test_two_profiles_distinct_state_concurrent(
    two_profile_archon_setup: dict[str, Path],
    tmp_archon_repo: Path,
) -> None:
    """R1 M3 — Sales and Engineering run concurrently with zero cross-contamination.

    The PRD §10.6 promise — "Sales and Engineering profiles run distinct
    Archon workflows simultaneously" — is the explicit acceptance gate
    Phase 5 has to clear before owner spins up the Sales Homie. This test
    is the gate.

    Uses ``ProcessPoolExecutor`` (NOT thread) because subprocess env
    isolation needs distinct OS processes — threads share ``os.environ``
    and would race on the per-worker setenv.
    """
    sales_root = two_profile_archon_setup["sales"]
    eng_root = two_profile_archon_setup["engineering"]
    scripts_dir = str(Path(__file__).resolve().parent.parent)

    # ---- Spawn two workers concurrently ----
    futures = []
    with ProcessPoolExecutor(max_workers=2) as pool:
        futures.append(
            pool.submit(
                _concurrent_worker,
                str(sales_root),
                "sales",
                str(tmp_archon_repo),
                scripts_dir,
            )
        )
        futures.append(
            pool.submit(
                _concurrent_worker,
                str(eng_root),
                "engineering",
                str(tmp_archon_repo),
                scripts_dir,
            )
        )

        results: dict[str, dict] = {}
        for future in as_completed(futures, timeout=240):
            payload = future.result()
            results[payload["name"]] = payload

    assert "sales" in results
    assert "engineering" in results

    # ---- Both runs returned exit 0 ----
    for name, payload in results.items():
        assert payload["returncode"] == 0, (
            f"concurrent {name} smoke run failed: "
            f"returncode={payload['returncode']}\n"
            f"stdout (first 800): {payload['stdout'][:800]!r}\n"
            f"stderr (first 800): {payload['stderr'][:800]!r}"
        )

    # ---- Distinct on-disk state per profile ----
    sales_marker = _read_marker(sales_root)
    eng_marker = _read_marker(eng_root)
    assert "profile=sales" in sales_marker
    assert "profile=engineering" in eng_marker
    # Cross-contamination check — concurrent runs must NOT have raced into
    # writing each other's name to the wrong root.
    assert "profile=engineering" not in sales_marker, (
        f"sales marker contaminated by engineering during concurrent run! "
        f"Content: {sales_marker!r}"
    )
    assert "profile=sales" not in eng_marker, (
        f"engineering marker contaminated by sales during concurrent run! "
        f"Content: {eng_marker!r}"
    )

    # ---- Distinct ralph state per profile (R1 M6) ----
    sales_state = _read_ralph_state(sales_root)
    eng_state = _read_ralph_state(eng_root)
    assert sales_state["profile"] == "sales", (
        f"sales ralph state corrupted by concurrent run: {sales_state!r}"
    )
    assert eng_state["profile"] == "engineering", (
        f"engineering ralph state corrupted by concurrent run: {eng_state!r}"
    )

    # ---- Distinct config.yaml files (sanity — same as sequential) ----
    sales_cfg = sales_root / ".archon" / "config.yaml"
    eng_cfg = eng_root / ".archon" / "config.yaml"
    assert sales_cfg.is_file()
    assert eng_cfg.is_file()
    assert sales_cfg != eng_cfg

    # ---- Smoke YAML present under both ----
    assert (
        sales_root / ".archon" / "workflows" / "profile-isolation-smoke.yaml"
    ).is_file()
    assert (
        eng_root / ".archon" / "workflows" / "profile-isolation-smoke.yaml"
    ).is_file()

    # ---- No orphan worktrees (--no-worktree means worktrees/ stays empty) ----
    sales_worktrees = sales_root / ".archon" / "worktrees"
    eng_worktrees = eng_root / ".archon" / "worktrees"
    assert list(sales_worktrees.iterdir()) == []
    assert list(eng_worktrees.iterdir()) == []
