"""Attribution tests for the codex render adapter (video_imagegen).

Every codex process writes into ONE shared ``generated_images`` tree and the CLI
exposes no per-run output dir. The only handle on "which image is mine" is the
path codex prints on stdout. Three real defects were found here on 2026-07-09,
and a full batch came back as one image under six names:

  1. the fresh-png fallback fired before codex printed its path, so every
     still-polling worker copied whichever image landed first
  2. a claimed file stayed a ``.png`` under the watched root, so siblings
     rediscovered it (one file was claimed twice, ``.claimed-.claimed-``)
  3. solo-ness was read live, so a worker whose siblings had merely FINISHED
     re-enabled the fallback and swallowed a sibling's image

The fake codex below reproduces the shape that produced all three: a shared
tree, a decoy second file per session (real codex saves ``ig_*.png`` AND a
descriptive name), staggered finish times, and an agent that reports nothing.

No real CLI, no network, no images.
"""

from __future__ import annotations

import queue
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS))

import video_imagegen  # noqa: E402


class _FakeStdin:
    def write(self, b) -> None:
        pass

    def close(self) -> None:
        pass


class _LineStream:
    """Blocking iterator over lines, ended by a sentinel. Mimics proc.stdout."""

    def __init__(self) -> None:
        self._q: queue.Queue = queue.Queue()

    def emit(self, line: bytes) -> None:
        self._q.put(line)

    def end(self) -> None:
        self._q.put(None)

    def __iter__(self):
        while True:
            item = self._q.get()
            if item is None:
                return
            yield item


def _install_fake_codex(monkeypatch, tmp_path, plan: list[dict]):
    """Wire a fake codex whose Nth spawn behaves per ``plan[N]``.

    Each plan entry: {delay, payload, emit_path, emit_thread, decoy, regenerate}

    ``emit_thread`` reproduces ``codex exec --json``: a ``thread.started`` event
    naming the generated_images subdir this process writes into.
    """

    images_root = tmp_path / "generated"
    images_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(video_imagegen, "_generated_images_root", lambda: images_root)
    monkeypatch.setattr(video_imagegen, "_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(video_imagegen.subprocess, "run", lambda *a, **k: None)

    spawn_lock = threading.Lock()
    spawns = {"n": 0}

    class _Popen:
        def __init__(self, cmd, stdin=None, stdout=None, stderr=None, **kw):
            with spawn_lock:
                idx = spawns["n"]
                spawns["n"] += 1
            self._spec = plan[idx]
            self.pid = 9000 + idx
            self.stdin = _FakeStdin()
            self.stdout = _LineStream()
            self._exited = threading.Event()
            self._thread_id = f"019f4874-1269-7da3-a677-57a27362000{idx}"
            self._session = images_root / self._thread_id
            threading.Thread(target=self._work, daemon=True).start()

        def _work(self) -> None:
            spec = self._spec
            if spec.get("emit_thread", True):
                self.stdout.emit(
                    b'{"type":"thread.started","thread_id":"'
                    + self._thread_id.encode()
                    + b'"}\n'
                )
            time.sleep(spec["delay"])
            self._session.mkdir(parents=True, exist_ok=True)
            payload = spec["payload"]
            png = self._session / f"ig_{payload}.png"
            png.write_bytes(b"\x89PNG" + payload.encode())
            if spec.get("decoy"):
                # real codex also saves a descriptive copy of the SAME bytes
                (self._session / f"pretty-{payload}.png").write_bytes(
                    b"\x89PNG" + payload.encode()
                )
            if spec.get("regenerate"):
                # the agent self-rejects a render and produces a better one
                time.sleep(spec["delay"])
                png = self._session / f"ig_{payload}_v2.png"
                png.write_bytes(b"\x89PNG" + payload.encode() + b"-final")
            if spec.get("emit_path", True):
                self.stdout.emit(str(png).encode() + b"\n")
            self.stdout.end()
            self._exited.set()

        def poll(self):
            return 0 if self._exited.is_set() else None

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr(video_imagegen.subprocess, "Popen", _Popen)
    return images_root


def _render(root: Path) -> Path | None:
    roots = [root]
    before = video_imagegen._snapshot_pngs(roots)
    return video_imagegen._run_codex_watching(["codex"], "prompt", roots, before, 20)


def _batch(root: Path, n: int) -> list[Path | None]:
    with ThreadPoolExecutor(max_workers=n) as ex:
        return list(ex.map(lambda _: _render(root), range(n)))


# =============================================================================
# THE regression test: N prompts must never collapse into one image
# =============================================================================


def test_concurrent_batch_returns_one_distinct_image_per_render(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Defects 1-4 at once: staggered finishes, decoy duplicates, a shared tree,
    and agents that regenerate. Thread scoping must keep every render separate."""

    monkeypatch.setenv("IMAGEGEN_MAX_CONCURRENCY", "3")
    plan = [
        {"delay": 0.30, "payload": "alpha", "decoy": True},
        {"delay": 0.05, "payload": "bravo", "decoy": True, "regenerate": True},
        {"delay": 0.15, "payload": "charlie", "decoy": True},
    ]
    root = _install_fake_codex(monkeypatch, tmp_path, plan)

    results = _batch(root, 3)

    assert all(r is not None for r in results), results
    payloads = [p.read_bytes() for p in results]
    assert len(set(payloads)) == 3, f"images collapsed: {payloads}"
    # each claimed file came out of its OWN thread dir
    sessions = {p.parent.parent.name for p in results}
    assert len(sessions) == 3, sessions
    # the regenerated image wins over the one the agent self-rejected
    assert b"\x89PNGbravo-final" in payloads


def test_thread_scope_ignores_a_siblings_image_entirely(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A fast sibling's png sits in the shared tree the whole time. A render must
    never see it -- only its own thread dir."""

    monkeypatch.setenv("IMAGEGEN_MAX_CONCURRENCY", "2")
    plan = [
        {"delay": 0.02, "payload": "fast"},
        {"delay": 0.40, "payload": "slow"},
    ]
    root = _install_fake_codex(monkeypatch, tmp_path, plan)

    results = _batch(root, 2)

    assert all(r is not None for r in results), results
    assert {p.read_bytes() for p in results} == {b"\x89PNGfast", b"\x89PNGslow"}


def test_claimed_image_is_invisible_to_siblings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Defect 2. A claim must remove the file from every fresh-png view."""

    root = tmp_path / "generated"
    (root / "sess-0").mkdir(parents=True)
    png = root / "sess-0" / "ig_x.png"
    png.write_bytes(b"\x89PNG x")

    assert png in video_imagegen._snapshot_pngs([root])

    claimed = video_imagegen._claim(png)

    assert claimed is not None and claimed.is_file()
    assert video_imagegen._snapshot_pngs([root]) == set()
    assert video_imagegen._newest_new_png([root], set()) is None


def test_second_claim_of_one_file_loses(tmp_path: Path) -> None:
    """Two renders can never both own the same file."""

    root = tmp_path / "generated" / "sess-0"
    root.mkdir(parents=True)
    png = root / "ig_x.png"
    png.write_bytes(b"\x89PNG x")

    first = video_imagegen._claim(png)
    second = video_imagegen._claim(png)

    assert first is not None
    assert second is None


def test_silent_agent_is_still_attributed_via_its_thread_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """codex often prints no path at all. The thread_id still identifies its dir,
    so a silent agent is attributable -- and gets ITS OWN image, not a sibling's."""

    monkeypatch.setenv("IMAGEGEN_MAX_CONCURRENCY", "2")
    plan = [
        {"delay": 0.05, "payload": "alpha", "emit_path": False},
        {"delay": 0.10, "payload": "bravo", "emit_path": False},
    ]
    root = _install_fake_codex(monkeypatch, tmp_path, plan)

    results = _batch(root, 2)

    assert all(r is not None for r in results), results
    assert {p.read_bytes() for p in results} == {b"\x89PNGalpha", b"\x89PNGbravo"}


def test_unscoped_concurrent_render_returns_none_not_a_siblings_image(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With neither a thread_id nor a reported path, a concurrent render has NO
    per-process handle on its output. It must fail loudly rather than adopt
    whichever image happens to be on disk."""

    monkeypatch.setenv("IMAGEGEN_MAX_CONCURRENCY", "2")
    plan = [
        {"delay": 0.05, "payload": "alpha", "emit_path": False, "emit_thread": False},
        {"delay": 0.10, "payload": "bravo", "emit_path": False, "emit_thread": False},
    ]
    root = _install_fake_codex(monkeypatch, tmp_path, plan)

    results = _batch(root, 2)

    assert results == [None, None], results


def test_solo_render_may_use_the_fresh_png_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The unscoped fallback is not dead code: with one render in flight, and no
    thread_id and no reported path, it is still sound."""

    plan = [{"delay": 0.05, "payload": "alpha", "emit_path": False, "emit_thread": False}]
    root = _install_fake_codex(monkeypatch, tmp_path, plan)

    result = _render(root)

    assert result is not None
    assert result.read_bytes() == b"\x89PNGalpha"


def test_last_message_file_wins_over_the_thread_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """codex's -o file names the image the agent FINALLY chose."""

    root = tmp_path / "generated"
    sess = root / "019f4874-1269-7da3-a677-57a273620000"
    sess.mkdir(parents=True)
    rejected = sess / "ig_first.png"
    rejected.write_bytes(b"\x89PNG rejected")
    chosen = sess / "ig_second.png"
    chosen.write_bytes(b"\x89PNG chosen")

    last = tmp_path / "last.txt"
    last.write_text(str(chosen), encoding="utf-8")

    assert video_imagegen._png_from_last_message(last) == chosen
    assert video_imagegen._png_from_last_message(tmp_path / "missing.txt") is None


def test_regenerated_image_wins_when_the_agent_self_rejects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The first settled png is the one the agent THREW AWAY. Taking it would ship
    the render it rejected. Only accept a scope-only candidate once the run is over
    or it has sat unchanged for the grace period."""

    monkeypatch.setattr(video_imagegen, "_REGEN_GRACE_S", 5.0)  # never reached here
    plan = [{"delay": 0.05, "payload": "alpha", "regenerate": True, "emit_path": False}]
    root = _install_fake_codex(monkeypatch, tmp_path, plan)

    result = _render(root)

    assert result is not None
    assert result.read_bytes() == b"\x89PNGalpha-final"


def test_hung_agent_yields_its_image_after_the_grace_period(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """codex can render, then hang on cleanup forever. We must not wait for exit."""

    monkeypatch.setattr(video_imagegen, "_REGEN_GRACE_S", 0.05)

    images_root = tmp_path / "generated"
    thread_id = "019f4874-1269-7da3-a677-57a273620000"
    monkeypatch.setattr(video_imagegen, "_generated_images_root", lambda: images_root)
    monkeypatch.setattr(video_imagegen, "_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(video_imagegen.subprocess, "run", lambda *a, **k: None)

    class _HangingPopen:
        def __init__(self, cmd, stdin=None, stdout=None, stderr=None, **kw):
            self.pid = 4242
            self.stdin = _FakeStdin()
            self.stdout = _LineStream()
            self.stdout.emit(
                b'{"type":"thread.started","thread_id":"' + thread_id.encode() + b'"}\n'
            )
            sess = images_root / thread_id
            sess.mkdir(parents=True, exist_ok=True)
            (sess / "ig_alpha.png").write_bytes(b"\x89PNGalpha")
            # never emits end-of-stream, never exits

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr(video_imagegen.subprocess, "Popen", _HangingPopen)

    result = _render(images_root)

    assert result is not None
    assert result.read_bytes() == b"\x89PNGalpha"


def test_reported_path_outside_our_thread_dir_is_rejected(tmp_path: Path) -> None:
    """OBSERVED 2026-07-09: two concurrent renders returned byte-identical images.

    codex runs with sandbox bypass and shells out to hunt for its own output when
    it loses track of it. It found a SIBLING's png and reported that path -- into
    the -o file only this run can read. A private channel does not make the path
    private. Attribution must be checked against the thread dir.
    """

    root = tmp_path / "generated"
    mine_id = "019f4874-1269-7da3-a677-57a273620000"
    theirs_id = "019f4874-1269-7da3-a677-57a273620001"
    (root / mine_id).mkdir(parents=True)
    (root / theirs_id).mkdir(parents=True)

    mine = root / mine_id / "ig_mine.png"
    mine.write_bytes(b"\x89PNG mine")
    theirs = root / theirs_id / "ig_theirs.png"
    theirs.write_bytes(b"\x89PNG theirs")

    assert video_imagegen._within_thread(mine, [root], mine_id) is True
    assert video_imagegen._within_thread(theirs, [root], mine_id) is False
    # a claimed file stays under its own thread dir
    claimed = video_imagegen._claim(mine)
    assert video_imagegen._within_thread(claimed, [root], mine_id) is True
    # with no thread id nothing can be attributed
    assert video_imagegen._within_thread(theirs, [root], None) is False


def test_a_lie_in_the_output_file_cannot_serve_a_siblings_image(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The -o file is model-authored. A path it names is a CLAIM, not evidence.

    The previous version of this test let the thief write its own png BEFORE the
    lie, so the directory scan satisfied the watcher and the lie was never
    consulted -- it passed with the guard removed, which means it tested nothing.
    Per the falsifying sequence: the process must have NO valid in-scope png of
    its own at the moment the lie lands, or the lie is never load-bearing.

    Runs the identical scenario twice and asserts the outcomes DIFFER, so a
    future edit that neuters _within_thread cannot leave this test green.
    """

    images_root = tmp_path / "generated"
    images_root.mkdir()
    monkeypatch.setattr(video_imagegen, "_generated_images_root", lambda: images_root)
    monkeypatch.setattr(video_imagegen, "_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(video_imagegen, "_REGEN_GRACE_S", 0.02)
    monkeypatch.setattr(video_imagegen.subprocess, "run", lambda *a, **k: None)

    ours_id = "019f4874-1269-7da3-a677-57a273620000"
    sibling_id = "019f4874-1269-7da3-a677-57a273620001"

    def _scenario(*, guard_enabled: bool) -> Path | None:
        """One render. It never produces a png; its -o names a sibling's png."""
        for d in (images_root / ours_id, images_root / sibling_id):
            shutil.rmtree(d, ignore_errors=True)
        sibling_png = images_root / sibling_id / "ig_sibling.png"
        sibling_png.parent.mkdir(parents=True)
        sibling_png.write_bytes(b"\x89PNGsibling")

        class _Popen:
            def __init__(self, cmd, stdin=None, stdout=None, stderr=None, **kw):
                self.pid = 7100
                self.stdin = _FakeStdin()
                self.stdout = _LineStream()
                self._exited = threading.Event()
                self._o = Path(cmd[cmd.index("-o") + 1]) if "-o" in cmd else None
                threading.Thread(target=self._work, daemon=True).start()

            def _work(self) -> None:
                self.stdout.emit(
                    b'{"type":"thread.started","thread_id":"'
                    + ours_id.encode()
                    + b'"}\n'
                )
                time.sleep(0.05)
                # We render NOTHING of our own. Then we lie.
                if self._o is not None:
                    self._o.write_text(str(sibling_png), encoding="utf-8")
                self.stdout.end()
                self._exited.set()

            def poll(self):
                return 0 if self._exited.is_set() else None

            def wait(self, timeout=None):
                return 0

            def kill(self):
                pass

        with monkeypatch.context() as m:
            m.setattr(video_imagegen.subprocess, "Popen", _Popen)
            if not guard_enabled:
                m.setattr(video_imagegen, "_within_thread", lambda *a, **k: True)
            roots = [images_root]
            before = video_imagegen._snapshot_pngs(roots)
            o = tmp_path / f"last-{guard_enabled}.txt"
            return video_imagegen._run_codex_watching(
                ["codex", "-o", str(o)], "p", roots, before, 3, last_message_file=o
            )

    # Control: with the guard neutered the lie IS believed. If this ever stops
    # holding, the scenario no longer reaches the guard and the assert below is
    # vacuous -- exactly the defect this test replaces.
    stolen = _scenario(guard_enabled=False)
    assert stolen is not None, "scenario never exercised the guard"
    assert stolen.read_bytes() == b"\x89PNGsibling"

    # The property: a reported path outside our own thread dir is never served.
    assert _scenario(guard_enabled=True) is None


def test_thread_id_parsed_from_json_event() -> None:
    line = '{"type":"thread.started","thread_id":"019f4874-1269-7da3-a677-57a2736caee7"}'
    assert (
        video_imagegen._thread_id_from_stdout(line)
        == "019f4874-1269-7da3-a677-57a2736caee7"
    )
    assert video_imagegen._thread_id_from_stdout('{"type":"turn.started"}') is None
    assert video_imagegen._thread_id_from_stdout("") is None


# =============================================================================
# Rule 1: the concurrency bound is resolved at call time
# =============================================================================


def test_max_concurrency_resolved_at_call_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IMAGEGEN_MAX_CONCURRENCY", raising=False)
    assert video_imagegen._max_concurrency() == 3

    monkeypatch.setenv("IMAGEGEN_MAX_CONCURRENCY", "6")
    assert video_imagegen._max_concurrency() == 6

    monkeypatch.setenv("IMAGEGEN_MAX_CONCURRENCY", "garbage")
    assert video_imagegen._max_concurrency() == 3

    monkeypatch.setenv("IMAGEGEN_MAX_CONCURRENCY", "0")
    assert video_imagegen._max_concurrency() == 1


def test_concurrency_gate_never_exceeds_the_bound(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """MAX=1 admits exactly one codex render at a time."""

    monkeypatch.setenv("IMAGEGEN_MAX_CONCURRENCY", "1")
    plan = [{"delay": 0.05, "payload": p} for p in ("alpha", "bravo", "charlie")]
    root = _install_fake_codex(monkeypatch, tmp_path, plan)

    peak = {"n": 0}
    real_solo = video_imagegen._solo

    def _watch_solo() -> bool:
        peak["n"] = max(peak["n"], video_imagegen._INFLIGHT)
        return real_solo()

    monkeypatch.setattr(video_imagegen, "_solo", _watch_solo)

    results = _batch(root, 3)

    assert all(r is not None for r in results)
    assert peak["n"] == 1, f"renders overlapped: peak inflight={peak['n']}"


# =============================================================================
# The `instruction` override must actually reach the CLI (it was dead code)
# =============================================================================


def _capture_instruction(monkeypatch, tmp_path, **kw):
    """Run one generation against a stubbed CLI; return the instruction it got."""
    seen = {}

    def _fake_watch(cmd, instruction, roots, before, timeout, **_):
        seen["instruction"] = instruction
        seen["cmd"] = cmd
        png = tmp_path / "out.png"
        png.write_bytes(b"\x89PNG")
        return png

    monkeypatch.setattr(video_imagegen.shutil, "which", lambda _: "codex")
    monkeypatch.setattr(video_imagegen, "_run_codex_watching", _fake_watch)
    monkeypatch.setattr(video_imagegen, "_snapshot_pngs", lambda roots: set())
    video_imagegen.generate_image(
        "a scene", {}, "1:1", str(tmp_path / "assets"), name="x", **kw
    )
    return seen


def test_verbatim_instruction_override_reaches_the_cli(monkeypatch, tmp_path) -> None:
    """A caller-supplied instruction must be sent AS-IS.

    build_instruction() appends "Absolutely no text, no words, no letters" -- right
    for a video frame, fatal for an ad whose copy is baked into the pixels. The
    override was accepted, documented, and then unconditionally overwritten, so
    every marketing render silently came back with no text on it.
    """
    mine = "Render a poster with the headline QUOTES IN 60 SECONDS baked in."
    seen = _capture_instruction(monkeypatch, tmp_path, instruction=mine)

    assert seen["instruction"] == mine
    assert "Absolutely no text" not in seen["instruction"]


def test_instruction_none_still_builds_the_default(monkeypatch, tmp_path) -> None:
    """The None sentinel keeps the pre-existing behaviour byte-for-byte."""
    seen = _capture_instruction(monkeypatch, tmp_path)

    assert seen["instruction"] == video_imagegen.build_instruction("a scene", {}, "1:1")
    assert "Absolutely no text" in seen["instruction"]


def test_refs_still_append_identity_lock_to_an_override(monkeypatch, tmp_path) -> None:
    """An override opts out of build_instruction, NOT out of the identity lock."""
    ref = tmp_path / "ref.png"
    ref.write_bytes(b"\x89PNG")
    seen = _capture_instruction(
        monkeypatch, tmp_path, instruction="my prompt", refs=[str(ref)]
    )

    assert seen["instruction"].startswith("my prompt")
    assert video_imagegen._IDENTITY_LOCK_LINE in seen["instruction"]
    assert "-i" in seen["cmd"] and str(ref) in seen["cmd"]
