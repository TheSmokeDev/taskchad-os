"""Tests for the new PRP-WF1B publication-binding module.

Real-Git adversarial fixtures follow the `_git`/`_repo` precedent in
`test_prp_artifact_contracts.py:33-59`. `gh` network calls are never made;
`gh pr create`/`gh pr list`/`gh repo view` are exercised through a fake `gh`
shim on PATH (or monkeypatched subprocess) since this suite has no GitHub
credentials or network access. A local bare repo stands in for `origin` so
real `git push`/`git ls-remote` plumbing is exercised without network I/O.
"""

from __future__ import annotations

import errno
import importlib.util
import inspect
import json
import os
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent / "prp_publication_binding.py"

spec = importlib.util.spec_from_file_location("prp_publication_binding", _SCRIPT)
pb = importlib.util.module_from_spec(spec)
sys.modules["prp_publication_binding"] = pb
spec.loader.exec_module(pb)
_REAL_PUBLISH_SEALED_TRANSACTION = pb.publish_sealed_transaction


@pytest.fixture(autouse=True)
def _offline_publication_target(monkeypatch):
    monkeypatch.setattr(
        pb, "_resolve_publication_target", lambda _identity: ("octo/demo", "main")
    )

    def publish_from_manifest_file(identity, artifacts_dir, manifest=None):
        if manifest is not None:
            (Path(artifacts_dir) / "approval-manifest.json").write_bytes(
                pb.canonical_json(manifest)
            )
        return _REAL_PUBLISH_SEALED_TRANSACTION(identity, artifacts_dir)

    monkeypatch.setattr(pb, "publish_sealed_transaction", publish_from_manifest_file)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t.test",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        shell=False,
    )


def _repo(tmp_path: Path, name: str = "repo") -> Path:
    root = tmp_path / name
    root.mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    # The contract's env-clean verification requires every fixture to carry
    # its own trusted local identity; command-scoped `-c` flags are not
    # visible to the later `git var GIT_*_IDENT` transaction step.
    _git(root, "config", "--local", "user.name", "t")
    _git(root, "config", "--local", "user.email", "t@t.test")
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")
    return root


def _worktree(main_root: Path, wt_path: Path, branch: str) -> Path:
    _git(main_root, "branch", branch)
    _git(main_root, "worktree", "add", str(wt_path), branch)
    return wt_path


class _FakeCompleted:
    def __init__(self, returncode: int, stdout_text: str = "", stderr_text: str = ""):
        self.returncode = returncode
        self.stdout = stdout_text.encode("utf-8")
        self.stderr = stderr_text.encode("utf-8")


class FakeGitHub:
    """Fakes `gh repo view`/`gh pr list`/`gh pr create` at the Python level
    (never a real subprocess) so the full classification/create/retry logic
    in `publish_sealed_transaction` is exercised without network access or
    GitHub credentials. Real `git` calls are never intercepted."""

    def __init__(self, repo_slug: str, default_branch: str, expected_commit_oid: str):
        self.repo_slug = repo_slug
        self.default_branch = default_branch
        self.expected_commit_oid = expected_commit_oid
        self.prs: list[dict] = []
        self.create_calls: list[dict] = []
        self.list_calls: list[list[str]] = []
        self._next_number = 1
        self.force_ambiguous_list = False
        self.fail_first_create = False
        self._create_attempts = 0

    def __call__(self, args, **kwargs):
        assert args[0] == "gh", args
        assert kwargs.get("timeout") == pb._NETWORK_TIMEOUT_SECONDS
        if args[1:3] == ["repo", "view"]:
            return _FakeCompleted(
                0,
                json.dumps(
                    {
                        "nameWithOwner": self.repo_slug,
                        "defaultBranchRef": {"name": self.default_branch},
                    }
                ),
            )
        if args[1:3] == ["pr", "list"]:
            self.list_calls.append(list(args))
            if self.force_ambiguous_list:
                return _FakeCompleted(0, "not-json-at-all")
            head = args[args.index("--head") + 1]
            base = args[args.index("--base") + 1]
            _, _, short_branch = head.partition(":")
            matches = [
                pr
                for pr in self.prs
                if pr["headRefName"] == short_branch and pr["baseRefName"] == base
            ]
            return _FakeCompleted(0, json.dumps(matches))
        if args[1:3] == ["pr", "create"]:
            self.create_calls.append(
                {
                    "args": list(args),
                    "input": kwargs.get("input"),
                    "shell": kwargs.get("shell"),
                    "timeout": kwargs.get("timeout"),
                }
            )
            self._create_attempts += 1
            if self.fail_first_create and self._create_attempts == 1:
                return _FakeCompleted(1, "", "simulated transient create failure")
            head = args[args.index("--head") + 1]
            base = args[args.index("--base") + 1]
            title = args[args.index("--title") + 1]
            owner, _, short_branch = head.partition(":")
            body_bytes = kwargs.get("input") or b""
            number = self._next_number
            self._next_number += 1
            self.prs.append(
                {
                    "number": number,
                    "url": f"https://github.com/{self.repo_slug}/pull/{number}",
                    "headRefName": short_branch,
                    "headRefOid": self.expected_commit_oid,
                    "headRepositoryOwner": {"login": owner},
                    "headRepository": {"name": self.repo_slug.split("/", 1)[1]},
                    "baseRefName": base,
                    "title": title,
                    "body": body_bytes.decode("utf-8"),
                }
            )
            return _FakeCompleted(0, "")
        raise AssertionError(f"unexpected gh invocation: {args}")


def _install_fake_gh(monkeypatch, fake_gh: FakeGitHub) -> None:
    real_run = subprocess.run

    def fake_run(args, *a, **kw):
        if args and args[0] == "gh":
            return fake_gh(args, **kw)
        return real_run(args, *a, **kw)

    monkeypatch.setattr(pb.subprocess, "run", fake_run)


def _publish_fixture(
    tmp_path, monkeypatch, *, repo_slug="octo/demo", default_branch="main"
):
    """Real main repo + real local bare `origin` + real linked worktree +
    a real sealed transaction from `build_sealed_transaction`, completed
    into a full schema-2 approval-manifest.json-shaped dict (the two fields
    `build_sealed_transaction` deliberately leaves for the package-gate
    orchestrator: `pr_base_branch` and `repository.repository_slug`)."""
    bare = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", default_branch, str(bare)],
        check=True,
        shell=False,
    )
    main_root = _repo(tmp_path, "main")
    _git(main_root, "branch", "-M", default_branch)
    _git(main_root, "remote", "add", "origin", str(bare))
    _git(main_root, "push", "-q", "origin", default_branch)
    wt = _worktree(main_root, tmp_path / "wt", "feature")
    identity = pb.discover_repository(wt)
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()

    (wt / "a.txt").write_text("approved content\n", encoding="utf-8")
    package_bytes = json.dumps(
        {"title": "Approved change", "commit_message": "approved change"}
    ).encode("utf-8")
    body_bytes = b"PR body approved\n"
    monkeypatch.setattr(
        pb,
        "_resolve_publication_target",
        lambda _identity: (repo_slug, default_branch),
    )
    tx = pb.build_sealed_transaction(
        identity,
        artifacts_dir,
        package_bytes,
        body_bytes,
        ["a.txt"],
    )

    payload = dict(tx.manifest)
    approval_digest_value = pb.approval_digest(payload)
    transaction_metadata = json.loads((tx.directory / "transaction.json").read_bytes())
    manifest = {
        "schema": 2,
        "approval_revision": f"2:{payload['run_id']}:{approval_digest_value[:16]}",
        "approval_digest": approval_digest_value,
        "payload": payload,
        "sealed_transaction": {
            "path": f"publication-transactions/{payload['run_id']}.sealed",
            "package": "sealed-pr-package.json",
            "body": "sealed-pr-body.md",
            "metadata": "transaction.json",
            "object_count": transaction_metadata["object_count"],
        },
    }
    (artifacts_dir / "approval-manifest.json").write_bytes(pb.canonical_json(manifest))
    fake_gh = FakeGitHub(repo_slug, default_branch, payload["commit_oid"])
    _install_fake_gh(monkeypatch, fake_gh)
    # The real classifier requires a canonical GitHub origin URL. This fixture
    # keeps a local bare origin so ls-remote/push exercise real Git without
    # network access; the parser itself is covered independently.
    monkeypatch.setattr(pb, "_derive_origin_repository_slug", lambda root: repo_slug)
    # Publication identity requires a canonical GitHub origin slug, while the
    # network-free fixture intentionally keeps origin as a local bare repo so
    # real push/lease/ls-remote plumbing can be exercised.
    monkeypatch.setattr(pb, "_derive_origin_repository_slug", lambda _root: repo_slug)
    return {
        "identity": identity,
        "artifacts_dir": artifacts_dir,
        "manifest": manifest,
        "payload": payload,
        "tx": tx,
        "fake_gh": fake_gh,
        "bare": bare,
        "main_root": main_root,
        "wt": wt,
    }


def test_binding_vector_binds_exact_package_and_body_bytes_title_message_and_revision():
    pkg = b'{"a":1}'
    body = b"pr body text\n"
    pkg_digest = pb.package_bytes_digest(pkg)
    body_digest = pb.body_bytes_digest(body)
    assert len(pkg_digest) == 64 and int(pkg_digest, 16) >= 0
    assert len(body_digest) == 64 and int(body_digest, 16) >= 0
    # domain separation: same bytes through the wrong digest function differ
    assert pb.package_bytes_digest(pkg) != pb.body_bytes_digest(pkg)
    # a single mutated byte anywhere in the package changes the digest
    assert pb.package_bytes_digest(pkg + b" ") != pkg_digest
    assert pb.package_bytes_digest(pkg[:-1]) != pkg_digest

    payload = {
        "schema": 2,
        "run_id": "a" * 32,
        "package_bytes_digest": pkg_digest,
        "body_bytes_digest": body_digest,
        "title": "exact title",
        "commit_message": "exact message",
        "branch_ref": "refs/heads/x",
    }
    digest_a = pb.approval_digest(payload)
    assert len(digest_a) == 64
    # title mutation changes approval_digest (binds title)
    assert pb.approval_digest({**payload, "title": "different title"}) != digest_a
    # commit message mutation changes approval_digest (binds message)
    assert pb.approval_digest({**payload, "commit_message": "different"}) != digest_a
    # run_id mutation changes approval_digest (binds revision/run_id)
    assert pb.approval_digest({**payload, "run_id": "b" * 32}) != digest_a
    # package/body digest mutation changes approval_digest (binds package/body bytes)
    assert pb.approval_digest({**payload, "package_bytes_digest": "0" * 64}) != digest_a
    assert pb.approval_digest({**payload, "body_bytes_digest": "0" * 64}) != digest_a
    # canonical JSON is deterministic regardless of input key order
    reordered = {k: payload[k] for k in reversed(list(payload))}
    assert pb.approval_digest(reordered) == digest_a
    with pytest.raises(pb.CanonicalJsonError, match="duplicate JSON key"):
        pb._strict_json_loads('{"a":1,"a":2}')
    with pytest.raises(pb.CanonicalJsonError):
        pb._strict_json_loads('{"a":1.5}')


def test_object_inventory_digest_vectors_and_rejections():
    empty_digest = pb.object_inventory_digest([])
    assert (
        empty_digest
        == "0c1fd15bd502a782875d336536131e6ac86959f9bfeeccc4ed030ea6a0c71757"
    )

    one_record = [("blob", "0" * 40, b"hello\n")]
    one_digest = pb.object_inventory_digest(one_record)
    assert (
        one_digest == "25ab149c21f502feae25a65cb4098c77b174154f40ba34df49c8ba5ddf38e6b0"
    )

    two_records = one_record + [("tree", "f" * 40, b"100644 a\x00" + bytes(20))]
    two_digest = pb.object_inventory_digest(two_records)
    assert (
        two_digest == "c699ddcd6fbcc8617c061cd8e0f649c150e321b35b907240d4cb67272d1891c2"
    )

    # verify_object_inventory accepts a valid, hash-verified, sorted inventory
    # built from real Git object bytes (the fixed vectors above use synthetic
    # all-zero/all-f OIDs independent of real hashing, per PRP §6; hash
    # verification itself is exercised here against a real SHA-1 blob hash).
    real_oid = (
        "ce013625030ba8dba906f756967f9e9ca394464a"  # `git hash-object` of b"hello\n"
    )
    real_record = [("blob", real_oid, b"hello\n")]
    assert pb.verify_object_inventory(
        real_record, "sha1"
    ) == pb.object_inventory_digest(real_record)

    # reordered (not sorted by raw OID bytes) is rejected by the verifier,
    # even though the low-level framer would happily hash it differently.
    other_oid = "f" * 40
    two_real = real_record + [("tree", other_oid, b"100644 a\x00" + bytes(20))]
    reordered = [two_real[1], two_real[0]]
    assert pb.object_inventory_digest(reordered) != pb.object_inventory_digest(two_real)
    with pytest.raises(pb.CanonicalJsonError):
        pb.verify_object_inventory(reordered, "sha1")

    # duplicate OID rejected
    with pytest.raises(pb.CanonicalJsonError):
        pb.verify_object_inventory([real_record[0], real_record[0]], "sha1")

    # unsupported type rejected
    with pytest.raises(pb.CanonicalJsonError):
        pb.verify_object_inventory([("blorb", real_oid, b"hello\n")], "sha1")

    # OID that does not hash-verify against type/size/content is rejected
    with pytest.raises(pb.CanonicalJsonError):
        pb.verify_object_inventory([("blob", "1" * 40, b"hello\n")], "sha1")

    # length-endian variant: swapping the record_count encoding to
    # little-endian must not silently produce the same digest (count=1 is
    # asymmetric between big- and little-endian, unlike count=0).
    import hashlib

    be_bytes = pb._canonical_object_inventory_bytes(one_record)
    le_bytes = (
        b"taskchad:git-object-inventory:v2\0"
        + (1).to_bytes(8, "little")
        + be_bytes[len(b"taskchad:git-object-inventory:v2\0") + 8 :]
    )
    assert hashlib.sha256(le_bytes).hexdigest() != one_digest

    # compressed-content variant: a zlib-compressed loose object stream must
    # not be accepted as canonical_content (framing uses the raw payload,
    # not the deflate-compressed loose object bytes).
    import zlib

    compressed = zlib.compress(b"hello\n")
    assert pb.object_inventory_digest([("blob", "0" * 40, compressed)]) != one_digest


def test_manifest_presence_never_supplies_archon_approval(tmp_path, monkeypatch):
    import dataclasses

    fx = _publish_fixture(tmp_path, monkeypatch)

    for cls in (pb.PublicationResult, pb.SealedTransaction):
        fields = {f.name for f in dataclasses.fields(cls)}
        assert (
            "approved" not in fields
            and "ok_to_publish" not in fields
            and "approval" not in fields
        )

    result = pb.publish_sealed_transaction(fx["identity"], fx["artifacts_dir"])
    assert result.ok, result.detail
    assert (fx["artifacts_dir"] / "publish.json").exists()

    # presence of the same on-disk manifest is never trusted as authority by
    # itself: a replay re-derives everything from fresh live state and only
    # succeeds because that live state still genuinely matches (idempotent
    # resume), not because the manifest file's mere existence is honored.
    fresh_identity = pb.discover_repository(fx["wt"])
    result_again = pb.publish_sealed_transaction(
        fresh_identity, fx["artifacts_dir"], fx["manifest"]
    )
    assert result_again.ok, result_again.detail
    assert len(fx["fake_gh"].create_calls) == 1  # replay did not create a second PR


def test_trusted_discovery_succeeds_for_a_genuine_linked_worktree(tmp_path):
    main_root = _repo(tmp_path, "main")
    wt = _worktree(main_root, tmp_path / "wt", "feature")

    identity = pb.discover_repository(wt)
    assert identity.root == wt.resolve()
    assert identity.common_dir == (main_root / ".git").resolve()
    assert identity.git_dir != identity.common_dir
    assert identity.branch_ref == "refs/heads/feature"
    assert identity.object_format in ("sha1", "sha256")
    assert len(identity.baseline_commit) in (40, 64)
    assert len(identity.worktree_id) == 64
    int(identity.worktree_id, 16)

    # rediscovery from the same live state is stable/reproducible
    identity_again = pb.discover_repository(wt)
    assert identity_again == identity


def test_trusted_discovery_rejects_bare_repository(tmp_path):
    bare = tmp_path / "bare.git"
    bare.mkdir()
    _git(bare, "init", "-q", "--bare")
    with pytest.raises(pb.RepositoryIdentityError):
        pb.discover_repository(bare)


def test_trusted_discovery_rejects_detached_head(tmp_path):
    main_root = _repo(tmp_path, "main")
    wt = _worktree(main_root, tmp_path / "wt", "feature")
    head = _git(wt, "rev-parse", "HEAD").stdout.strip()
    _git(wt, "checkout", "--detach", head)
    with pytest.raises(pb.RepositoryIdentityError):
        pb.discover_repository(wt)


def test_trusted_discovery_rejects_main_checkout_where_gitdir_equals_common_dir(
    tmp_path,
):
    main_root = _repo(tmp_path, "main")
    with pytest.raises(pb.RepositoryIdentityError):
        pb.discover_repository(main_root)


def test_trusted_discovery_rejects_mutated_root_common_dir_and_replaced_gitdir(
    tmp_path,
):
    main_root = _repo(tmp_path, "main")
    wt = _worktree(main_root, tmp_path / "wt", "feature")
    assert pb.discover_repository(wt).root == wt.resolve()

    # "mutated root": copy the entire linked-worktree directory (including its
    # `.git` gitdir-pointer file) to a new path that was never registered by
    # `git worktree add`. The copy's toplevel resolves fine, but its root is
    # not a member of `git worktree list` for the shared common repository --
    # this is exactly the "root not registered" rejection in PRP §4.
    import shutil

    duplicate = tmp_path / "wt-duplicate"
    shutil.copytree(wt, duplicate)
    with pytest.raises(pb.RepositoryIdentityError):
        pb.discover_repository(duplicate)

    # "replaced gitdir": point the worktree's `.git` file at a foreign
    # repository's private worktree gitdir instead of its real one. The
    # foreign common repository never registered this path as a worktree,
    # so discovery must still fail closed.
    other_main = _repo(tmp_path, "other-main")
    other_wt = _worktree(other_main, tmp_path / "other-wt", "other-feature")
    foreign_gitdir = _git(
        other_wt, "rev-parse", "--path-format=absolute", "--git-dir"
    ).stdout.strip()

    tampered = tmp_path / "wt-tampered"
    shutil.copytree(wt, tampered)
    tampered_git_pointer = tampered / ".git"
    tampered_git_pointer.unlink()  # Windows will not open-for-write over the hidden pointer file
    tampered_git_pointer.write_text(f"gitdir: {foreign_gitdir}\n", encoding="utf-8")
    with pytest.raises(pb.RepositoryIdentityError):
        pb.discover_repository(tampered)


def test_trusted_discovery_rejects_symlinked_gitdir_component(tmp_path):
    main_root = _repo(tmp_path, "main")
    wt = _worktree(main_root, tmp_path / "wt", "feature")
    git_dir = Path(
        _git(wt, "rev-parse", "--path-format=absolute", "--git-dir").stdout.strip()
    )
    real_worktrees_dir = git_dir.parent
    try:
        real_target = real_worktrees_dir.rename(tmp_path / "real-worktrees-moved")
        real_worktrees_dir.symlink_to(real_target, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation not permitted in this environment")
    with pytest.raises(pb.RepositoryIdentityError):
        pb.discover_repository(wt)


def test_temp_index_tree_records_untracked_symlink_target_not_followed_bytes(tmp_path):
    main_root = _repo(tmp_path, "main")
    wt = _worktree(main_root, tmp_path / "wt", "feature")
    identity = pb.discover_repository(wt)
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()

    (wt / "real-target.txt").write_text(
        "real target content should NOT appear\n", encoding="utf-8"
    )
    link = wt / "linky"
    try:
        link.symlink_to("real-target.txt")
    except OSError:
        pytest.skip("symlink creation not permitted in this environment")

    package_bytes = json.dumps(
        {"title": "symlink title", "commit_message": "symlink commit"}
    ).encode("utf-8")
    tx = pb.build_sealed_transaction(
        identity,
        artifacts_dir,
        package_bytes,
        b"body\n",
        ["linky", "real-target.txt"],
    )
    entries = {
        e["new_path"]: e for e in tx.manifest["changed_entries"] if e["status"] != "R"
    }
    assert entries["linky"]["new_mode"] == "120000"

    env = pb._scoped_git_env(
        git_dir=identity.git_dir,
        work_tree=wt,
        index_file=tx.directory / "index",
        object_dir=tx.directory / "objects",
        alt_object_dirs=identity.common_dir / "objects",
    )
    blob = pb._run_scoped_git(
        ["cat-file", "-p", f"{tx.manifest['tree_oid']}:linky"], env, cwd=wt
    )
    assert (
        blob.stdout == b"real-target.txt"
    )  # target path bytes, never the followed content


def test_tree_digest_binds_mode_delete_rename_and_path_object_semantics(tmp_path):
    main_root = _repo(tmp_path, "main")
    (main_root / "modify.txt").write_text(
        "original content that is long enough\n" * 3, encoding="utf-8"
    )
    (main_root / "delete.txt").write_text("to be deleted\n", encoding="utf-8")
    (main_root / "rename-src.txt").write_text(
        "rename me please, keep most of this content intact\n" * 3, encoding="utf-8"
    )
    _git(main_root, "add", "-A")
    _git(main_root, "commit", "-q", "-m", "seed extra files")
    wt = _worktree(main_root, tmp_path / "wt", "feature")
    identity = pb.discover_repository(wt)
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()

    (wt / "modify.txt").write_text(
        "changed content that is long enough\n" * 3, encoding="utf-8"
    )
    (wt / "delete.txt").unlink()
    (wt / "rename-src.txt").rename(wt / "rename-dst.txt")

    package_bytes = json.dumps(
        {"title": "test title", "commit_message": "test commit"}
    ).encode("utf-8")
    allowed = ["modify.txt", "delete.txt", "rename-src.txt", "rename-dst.txt"]
    tx = pb.build_sealed_transaction(
        identity,
        artifacts_dir,
        package_bytes,
        b"pr body\n",
        allowed,
    )

    statuses = {e["status"] for e in tx.manifest["changed_entries"]}
    assert "M" in statuses
    assert (
        "D" in statuses or "R" in statuses
    )  # a well-matched rename subsumes the D+A pair

    changed_files = set(pb.changed_paths_projection(tx.manifest["changed_entries"]))
    assert "modify.txt" in changed_files
    assert {
        "rename-src.txt",
        "rename-dst.txt",
    } <= changed_files or "rename-dst.txt" in changed_files

    assert len(tx.manifest["tree_oid"]) in (40, 64)
    assert len(tx.manifest["object_state_digest"]) == 64
    assert tx.directory.exists()
    assert tx.directory.name.endswith(".sealed")

    # same content -> same tree_oid (deterministic); different content -> different tree_oid
    tx_again = pb.build_sealed_transaction(
        identity,
        artifacts_dir,
        package_bytes,
        b"pr body\n",
        allowed,
    )
    assert tx_again.manifest["tree_oid"] == tx.manifest["tree_oid"]

    (wt / "modify.txt").write_text("yet another change\n", encoding="utf-8")
    tx_changed = pb.build_sealed_transaction(
        identity,
        artifacts_dir,
        package_bytes,
        b"pr body\n",
        allowed,
    )
    assert tx_changed.manifest["tree_oid"] != tx.manifest["tree_oid"]


def test_existing_index_is_untouched_and_contamination_cannot_enter_tree(tmp_path):
    main_root = _repo(tmp_path, "main")
    (main_root / "a.txt").write_text("a original\n", encoding="utf-8")
    (main_root / "b.txt").write_text("b original\n", encoding="utf-8")
    _git(main_root, "add", "-A")
    _git(main_root, "commit", "-q", "-m", "seed a b")
    wt = _worktree(main_root, tmp_path / "wt", "feature")
    identity = pb.discover_repository(wt)
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()

    real_index_path = identity.git_dir / "index"
    _git(wt, "status")  # materialize the linked worktree's own index file
    before = real_index_path.read_bytes() if real_index_path.exists() else None

    (wt / "a.txt").write_text("a changed in-scope\n", encoding="utf-8")
    (wt / "b.txt").write_text("b changed OUT OF SCOPE\n", encoding="utf-8")

    package_bytes = json.dumps(
        {"title": "scoped title", "commit_message": "scoped commit"}
    ).encode("utf-8")
    tx = pb.build_sealed_transaction(
        identity,
        artifacts_dir,
        package_bytes,
        b"body\n",
        ["a.txt"],
    )

    after = real_index_path.read_bytes() if real_index_path.exists() else None
    assert before == after  # the user's real index is byte-identical

    changed_files = set(pb.changed_paths_projection(tx.manifest["changed_entries"]))
    assert "a.txt" in changed_files
    assert "b.txt" not in changed_files  # contamination outside allowed_paths excluded


def test_concurrent_edit_after_write_tree_cannot_change_approved_commit(tmp_path):
    main_root = _repo(tmp_path, "main")
    (main_root / "a.txt").write_text("original\n", encoding="utf-8")
    _git(main_root, "add", "-A")
    _git(main_root, "commit", "-q", "-m", "seed a")
    wt = _worktree(main_root, tmp_path / "wt", "feature")
    identity = pb.discover_repository(wt)
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()

    (wt / "a.txt").write_text("approved content\n", encoding="utf-8")
    package_bytes = json.dumps(
        {"title": "approved title", "commit_message": "approved"}
    ).encode("utf-8")
    tx = pb.build_sealed_transaction(
        identity,
        artifacts_dir,
        package_bytes,
        b"body\n",
        ["a.txt"],
    )
    approved_tree = tx.manifest["tree_oid"]
    approved_commit = tx.manifest["commit_oid"]

    # simulate a concurrent edit happening after the tree/commit were sealed
    (wt / "a.txt").write_text("mutated AFTER approval was computed\n", encoding="utf-8")

    env = pb._scoped_git_env(
        git_dir=identity.git_dir,
        work_tree=wt,
        index_file=tx.directory / "index",
        object_dir=tx.directory / "objects",
        alt_object_dirs=identity.common_dir / "objects",
    )
    result = pb._run_scoped_git(["cat-file", "-p", approved_commit], env, cwd=wt)
    assert result.returncode == 0
    assert f"tree {approved_tree}" in result.stdout.decode("utf-8")

    blob = pb._run_scoped_git(["cat-file", "-p", f"{approved_tree}:a.txt"], env, cwd=wt)
    assert blob.stdout == b"approved content\n"  # unaffected by the later mutation


def test_publish_never_invokes_add_commit_or_hooks(tmp_path, monkeypatch):
    fx = _publish_fixture(tmp_path, monkeypatch)
    calls = []
    prior_run = pb.subprocess.run

    def spy(args, *a, **kw):
        if args and args[0] == "git":
            calls.append(list(args))
        return prior_run(args, *a, **kw)

    monkeypatch.setattr(pb.subprocess, "run", spy)

    result = pb.publish_sealed_transaction(fx["identity"], fx["artifacts_dir"])
    assert result.ok, result.detail
    mutating_subcommands = {"add", "commit", "checkout", "stash"}
    for c in calls:
        assert len(c) < 2 or c[1] not in mutating_subcommands, c


def test_package_order_and_stable_reasons_have_zero_publication_side_effects(
    tmp_path, monkeypatch
):
    fx = _publish_fixture(tmp_path, monkeypatch)
    bad_manifest = json.loads(json.dumps(fx["manifest"]))
    bad_manifest["payload"]["title"] = (
        "TAMPERED TITLE"  # digest left stale -> binding fails
    )

    before_local = pb._run_plain_git(
        ["rev-parse", fx["identity"].branch_ref], fx["wt"]
    ).stdout
    before_remote = subprocess.run(
        ["git", "ls-remote", str(fx["bare"]), fx["identity"].branch_ref],
        capture_output=True,
        text=True,
        shell=False,
    ).stdout

    result = pb.publish_sealed_transaction(
        fx["identity"], fx["artifacts_dir"], bad_manifest
    )
    assert not result.ok
    assert result.reason == pb.PublicationReason.APPROVAL_BINDING_INVALID

    schema1_full_manifest = {
        "schema": 1,
        "baseline_head": fx["identity"].baseline_commit,
        "branch": "feature",
        "changed_files": ["a.txt"],
        "approved_diff_digest": "0" * 64,
    }
    result2 = pb.publish_sealed_transaction(
        fx["identity"], fx["artifacts_dir"], schema1_full_manifest
    )
    assert not result2.ok
    assert fx["fake_gh"].create_calls == []
    assert not (fx["artifacts_dir"] / "publish.json").exists()
    state = json.loads((fx["artifacts_dir"] / "publication-state.json").read_bytes())
    assert state["last_reason"] == pb.PublicationReason.APPROVAL_BINDING_INVALID

    after_local = pb._run_plain_git(
        ["rev-parse", fx["identity"].branch_ref], fx["wt"]
    ).stdout
    after_remote = subprocess.run(
        ["git", "ls-remote", str(fx["bare"]), fx["identity"].branch_ref],
        capture_output=True,
        text=True,
        shell=False,
    ).stdout
    assert before_local == after_local
    assert before_remote == after_remote
    assert fx["fake_gh"].create_calls == []
    assert not (fx["artifacts_dir"] / "publish.json").exists()


def test_publish_revalidates_all_binding_inputs_immediately_before_object_install(
    tmp_path, monkeypatch
):
    fx = _publish_fixture(tmp_path, monkeypatch)
    sealed_body_path = fx["tx"].directory / "sealed-pr-body.md"
    call_count = {"n": 0}
    real_validate = pb.validate_sealed_transaction

    def spy_validate(*a, **kw):
        call_count["n"] += 1
        result = real_validate(*a, **kw)
        if call_count["n"] == 1:
            # tamper with the sealed body between the first pass and row 8's
            # immediately-before-install repeat.
            os.chmod(sealed_body_path, 0o600)
            sealed_body_path.write_bytes(b"TAMPERED AFTER FIRST PASS\n")
            os.chmod(sealed_body_path, 0o400)
        return result

    monkeypatch.setattr(pb, "validate_sealed_transaction", spy_validate)

    result = pb.publish_sealed_transaction(fx["identity"], fx["artifacts_dir"])
    assert not result.ok
    assert result.reason == pb.PublicationReason.PRE_SIDE_EFFECT_REVALIDATION_FAILED
    assert call_count["n"] == 2
    assert fx["fake_gh"].create_calls == []
    assert not (fx["artifacts_dir"] / "publish.json").exists()


@pytest.mark.parametrize(
    "field,new_value",
    [
        ("title", "mutated title"),
        ("commit_message", "mutated message"),
        ("branch_ref", "refs/heads/other"),
        ("baseline_commit", "0" * 40),
        ("tree_oid", "1" * 40),
        ("commit_oid", "2" * 40),
        ("object_inventory_digest", "0" * 64),
        ("object_state_digest", "0" * 64),
        ("package_bytes_digest", "0" * 64),
        ("body_bytes_digest", "0" * 64),
        ("author_ident", "Mutated <m@m.test> 1 +0000"),
        ("committer_ident", "Mutated <m@m.test> 1 +0000"),
        ("timestamp", "1"),
        ("run_id", "f" * 32),
    ],
)
def test_mutation_of_each_bound_field_after_approval_fails_before_side_effect(
    tmp_path, monkeypatch, field, new_value
):
    fx = _publish_fixture(tmp_path, monkeypatch)
    tampered = json.loads(json.dumps(fx["manifest"]))
    # approval_digest/revision left stale, as an attacker would
    tampered["payload"][field] = new_value

    result = pb.publish_sealed_transaction(
        fx["identity"], fx["artifacts_dir"], tampered
    )
    assert not result.ok
    assert fx["fake_gh"].create_calls == []
    assert not (fx["artifacts_dir"] / "publish.json").exists()


def test_ref_compare_and_swap_and_push_lease_reject_concurrent_writer(
    tmp_path, monkeypatch
):
    fx = _publish_fixture(tmp_path, monkeypatch)
    wt = fx["wt"]
    old_head = pb._run_plain_git(["rev-parse", "HEAD"], wt).stdout.decode().strip()
    tree = pb._run_plain_git(["rev-parse", "HEAD^{tree}"], wt).stdout.decode().strip()
    concurrent_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "x",
        "GIT_AUTHOR_EMAIL": "x@x.test",
        "GIT_COMMITTER_NAME": "x",
        "GIT_COMMITTER_EMAIL": "x@x.test",
    }
    concurrent_commit = (
        subprocess.run(
            ["git", "commit-tree", tree, "-p", old_head],
            input=b"concurrent write\n",
            cwd=wt,
            capture_output=True,
            shell=False,
            env=concurrent_env,
        )
        .stdout.decode()
        .strip()
    )

    # local divergence: someone else moved refs/heads/feature via plumbing
    subprocess.run(
        ["git", "update-ref", "refs/heads/feature", concurrent_commit],
        cwd=wt,
        check=True,
        shell=False,
    )
    fresh_identity = pb.discover_repository(wt)
    result = pb.publish_sealed_transaction(
        fresh_identity, fx["artifacts_dir"], fx["manifest"]
    )
    assert not result.ok
    assert result.reason == pb.PublicationReason.LOCAL_REF_DIVERGED
    assert fx["fake_gh"].create_calls == []

    # restore local ref, then diverge on the REMOTE side only
    subprocess.run(
        ["git", "update-ref", "refs/heads/feature", old_head],
        cwd=wt,
        check=True,
        shell=False,
    )
    subprocess.run(
        ["git", "push", str(fx["bare"]), f"{concurrent_commit}:refs/heads/feature"],
        cwd=wt,
        check=True,
        shell=False,
    )
    fresh_identity2 = pb.discover_repository(wt)
    result2 = pb.publish_sealed_transaction(
        fresh_identity2, fx["artifacts_dir"], fx["manifest"]
    )
    assert not result2.ok
    assert result2.reason == pb.PublicationReason.REMOTE_DIVERGED
    assert fx["fake_gh"].create_calls == []


def test_retry_after_commit_or_push_is_idempotent_and_never_duplicates_pr(
    tmp_path, monkeypatch
):
    fx = _publish_fixture(tmp_path, monkeypatch)
    fx["fake_gh"].fail_first_create = True

    result = pb.publish_sealed_transaction(fx["identity"], fx["artifacts_dir"])
    assert result.ok, result.detail
    assert len(fx["fake_gh"].create_calls) == 2  # one reported failure + one retry
    assert len(fx["fake_gh"].prs) == 1  # never duplicated

    fresh_identity = pb.discover_repository(fx["wt"])
    result2 = pb.publish_sealed_transaction(
        fresh_identity, fx["artifacts_dir"], fx["manifest"]
    )
    assert result2.ok, result2.detail
    assert len(fx["fake_gh"].prs) == 1


def test_pr_create_uses_literal_body_file_dash_shell_false_and_exact_snapshot_stdin(
    tmp_path, monkeypatch
):
    fx = _publish_fixture(tmp_path, monkeypatch)
    result = pb.publish_sealed_transaction(
        fx["identity"], fx["artifacts_dir"], fx["manifest"]
    )
    assert result.ok, result.detail
    assert len(fx["fake_gh"].create_calls) == 1
    call = fx["fake_gh"].create_calls[0]
    args = call["args"]
    assert args[:3] == ["gh", "pr", "create"]
    idx = args.index("--body-file")
    assert args[idx + 1] == "-"
    assert call["shell"] is False
    assert call["timeout"] == pb._NETWORK_TIMEOUT_SECONDS
    assert call["input"] == b"PR body approved\n"


def test_body_path_swap_after_final_snapshot_cannot_change_created_pr_body(
    tmp_path, monkeypatch
):
    fx = _publish_fixture(tmp_path, monkeypatch)
    sealed_body_path = fx["tx"].directory / "sealed-pr-body.md"

    prior_run = pb.subprocess.run

    def swap_on_create(args, *a, **kw):
        if args and args[0] == "gh" and args[1:3] == ["pr", "create"]:
            os.chmod(sealed_body_path, 0o600)
            sealed_body_path.write_bytes(b"SWAPPED AFTER SNAPSHOT WAS TAKEN\n")
            os.chmod(sealed_body_path, 0o400)
        return prior_run(args, *a, **kw)

    monkeypatch.setattr(pb.subprocess, "run", swap_on_create)

    result = pb.publish_sealed_transaction(
        fx["identity"], fx["artifacts_dir"], fx["manifest"]
    )
    assert result.ok, result.detail
    assert fx["fake_gh"].create_calls[0]["input"] == b"PR body approved\n"
    assert fx["fake_gh"].prs[0]["body"] == "PR body approved\n"  # not the swapped text


def test_every_create_outcome_requeries_and_compares_unique_pr_body_utf8_bytes(
    tmp_path, monkeypatch
):
    fx = _publish_fixture(tmp_path, monkeypatch)
    original_call = FakeGitHub.__call__

    def report_failure_but_actually_create(self, args, **kwargs):
        result = original_call(self, args, **kwargs)
        if args[1:3] == ["pr", "create"]:
            return _FakeCompleted(1, "", "simulated transport failure")
        return result

    monkeypatch.setattr(FakeGitHub, "__call__", report_failure_but_actually_create)

    result = pb.publish_sealed_transaction(
        fx["identity"], fx["artifacts_dir"], fx["manifest"]
    )
    assert result.ok, result.detail
    # discovered via requery, not via gh's own reported exit code
    assert len(fx["fake_gh"].prs) == 1
    assert (
        len(fx["fake_gh"].create_calls) == 1
    )  # requery found "exact" -- no retry was needed


def test_post_create_body_mismatch_is_stable_and_never_mutates_remote_state(
    tmp_path, monkeypatch
):
    fx = _publish_fixture(tmp_path, monkeypatch)
    fake = fx["fake_gh"]
    payload = fx["payload"]
    short_branch = fx["identity"].branch_ref.removeprefix("refs/heads/")
    fake.prs.append(
        {
            "number": 99,
            "url": f"https://github.com/{fake.repo_slug}/pull/99",
            "headRefName": short_branch,
            "headRefOid": payload["commit_oid"],
            "headRepositoryOwner": {"login": "octo"},
            "headRepository": {"name": "demo"},
            "baseRefName": payload["pr_base_branch"],
            "title": payload["title"],
            "body": "SOMEONE ELSE'S BODY, NOT THE APPROVED ONE",
        }
    )

    result = pb.publish_sealed_transaction(
        fx["identity"], fx["artifacts_dir"], fx["manifest"]
    )
    assert not result.ok
    assert result.reason == pb.PublicationReason.REMOTE_PR_BODY_MISMATCH
    assert fake.create_calls == []
    assert not (fx["artifacts_dir"] / "publish.json").exists()

    result2 = pb.publish_sealed_transaction(
        pb.discover_repository(fx["wt"]), fx["artifacts_dir"], fx["manifest"]
    )
    assert not result2.ok
    assert result2.reason == pb.PublicationReason.REMOTE_PR_BODY_MISMATCH
    assert len(fake.prs) == 1  # never auto-mutated


def test_atomic_write_interruption_preserves_old_file_and_ignores_temp_leftover(
    tmp_path, monkeypatch
):
    dest = tmp_path / "artifact.json"
    pb.atomic_write(dest, b'{"a":1}')
    original = dest.read_bytes()

    def boom_replace(*a, **kw):
        raise OSError("simulated crash before replace")

    with monkeypatch.context() as m:
        m.setattr(pb.os, "replace", boom_replace)
        with pytest.raises(OSError):
            pb.atomic_write(dest, b'{"a":2}')
    assert dest.read_bytes() == original
    # atomic_write's own best-effort cleanup removed the temp file
    assert list(tmp_path.glob(".artifact.json.*.tmp")) == []

    # simulate a genuine crash where even best-effort cleanup could not run
    # (e.g. the process was killed): a `.{name}.{random}.tmp` leftover must
    # never be read as authority by a later write or by any reader.
    def boom_unlink(self, *a, **kw):
        raise OSError("cleanup also failed")

    with monkeypatch.context() as m:
        m.setattr(pb.os, "replace", boom_replace)
        m.setattr(pb.Path, "unlink", boom_unlink)
        with pytest.raises(OSError):
            pb.atomic_write(dest, b'{"a":3}')
    assert dest.read_bytes() == original
    leftovers = list(tmp_path.glob(".artifact.json.*.tmp"))
    assert len(leftovers) == 1
    leftover = leftovers[0]

    pb.atomic_write(dest, b'{"a":4}')
    assert dest.read_bytes() == b'{"a":4}'
    assert leftover.exists()  # untouched; never treated as authority
    leftover.unlink()

    # destination symlink/non-regular targets are rejected outright
    if sys.platform != "win32" or True:
        try:
            link = tmp_path / "linked.json"
            link.symlink_to(dest)
        except OSError:
            pytest.skip("symlink creation not permitted in this environment")
        with pytest.raises(OSError):
            pb.atomic_write(link, b"x")
        link.unlink()

    # overwrite=False refuses an existing destination
    with pytest.raises(FileExistsError):
        pb.atomic_write(dest, b'{"a":5}', overwrite=False)


def test_atomic_write_posix_and_windows_capability_branches_and_parent_fsync(
    tmp_path, monkeypatch
):
    dest = tmp_path / "artifact.json"
    pb.atomic_write(dest, b"data", mode=0o640)
    assert dest.read_bytes() == b"data"
    if sys.platform != "win32":
        assert stat.S_IMODE(dest.stat().st_mode) == 0o640

    # Windows: directory fsync is a documented no-op (advisory only)
    with monkeypatch.context() as m:
        m.setattr(pb.sys, "platform", "win32")
        pb._fsync_dir_best_effort(tmp_path)  # must not raise

    # POSIX-shaped advisory errno branches never raise
    for advisory_errno in (errno.EINVAL, errno.ENOTSUP, errno.EBADF):
        with monkeypatch.context() as m:
            m.setattr(pb.sys, "platform", "linux")
            m.setattr(pb.os, "open", lambda *a, **kw: 999)
            m.setattr(pb.os, "close", lambda fd: None)

            def raise_advisory(fd, _errno=advisory_errno):
                raise OSError(_errno, "advisory")

            m.setattr(pb.os, "fsync", raise_advisory)
            pb._fsync_dir_best_effort(tmp_path)  # must not raise

    # a non-advisory errno (e.g. EIO) must propagate as a real failure
    with monkeypatch.context() as m:
        m.setattr(pb.sys, "platform", "linux")
        m.setattr(pb.os, "open", lambda *a, **kw: 999)
        m.setattr(pb.os, "close", lambda fd: None)

        def raise_eio(fd):
            raise OSError(errno.EIO, "disk error")

        m.setattr(pb.os, "fsync", raise_eio)
        with pytest.raises(OSError):
            pb._fsync_dir_best_effort(tmp_path)

    # reader cap: a file over the byte cap is rejected
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * 10)
    assert pb.read_confined_regular_bytes(big, max_bytes=10) == b"x" * 10
    with pytest.raises(OSError):
        pb.read_confined_regular_bytes(big, max_bytes=9)


def test_lock_serializes_same_repo_branch_and_does_not_break_live_owner(tmp_path):
    artifacts_dir = tmp_path / "artifacts"
    common_dir = tmp_path / "repo" / ".git"
    common_dir.mkdir(parents=True)
    branch_ref = "refs/heads/main"

    with pb.acquire_repository_lock(artifacts_dir, common_dir, branch_ref, run_id="r1"):
        # real inter-process contention: a separate OS process trying the
        # same repository-scoped lock must be rejected while it is held.
        script = (
            "import importlib.util, sys\n"
            "spec = importlib.util.spec_from_file_location("
            f"'prp_publication_binding', {str(_SCRIPT)!r})\n"
            "pb = importlib.util.module_from_spec(spec)\n"
            "sys.modules['prp_publication_binding'] = pb\n"
            "spec.loader.exec_module(pb)\n"
            "try:\n"
            "    with pb.acquire_repository_lock("
            f"{str(artifacts_dir)!r}, {str(common_dir)!r}, {branch_ref!r}, run_id='r2'"
            "):\n"
            "        print('ACQUIRED')\n"
            "except pb.PublicationLockedError:\n"
            "    print('LOCKED')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, shell=False
        )
        assert "LOCKED" in result.stdout, (result.stdout, result.stderr)

        # in-process: a second thread attempting the same lock while this
        # thread holds it is also rejected immediately (keyed mutex).
        outcome = {}

        def try_acquire():
            try:
                with pb.acquire_repository_lock(
                    artifacts_dir, common_dir, branch_ref, run_id="r-thread"
                ):
                    outcome["thread"] = "ACQUIRED"
            except pb.PublicationLockedError:
                outcome["thread"] = "LOCKED"

        t = threading.Thread(target=try_acquire)
        t.start()
        t.join()
        assert outcome["thread"] == "LOCKED"

    # after release, a fresh acquisition succeeds -- a held lock's release
    # does not "break" a live owner elsewhere; it simply frees the lock.
    with pb.acquire_repository_lock(artifacts_dir, common_dir, branch_ref, run_id="r3"):
        pass

    # the on-disk record is diagnostic only: its mere existence from a prior
    # (now-released) acquisition never blocks a later live acquisition.
    lock_path = (
        artifacts_dir / "locks" / f"{pb._lock_name(common_dir, branch_ref)}.lock"
    )
    assert lock_path.exists()
    with pb.acquire_repository_lock(artifacts_dir, common_dir, branch_ref, run_id="r4"):
        pass


def test_schema1_approval_is_rejected_and_cannot_be_upgraded(tmp_path, monkeypatch):
    fx = _publish_fixture(tmp_path, monkeypatch)
    schema1_manifest_bytes = json.dumps(
        {
            "schema": 1,
            "baseline_head": fx["identity"].baseline_commit,
            "branch": "feature",
            "changed_files": ["a.txt"],
            "approved_diff_digest": "0" * 64,
        }
    ).encode("utf-8")
    result = pb.validate_sealed_transaction(
        fx["identity"],
        fx["artifacts_dir"],
        schema1_manifest_bytes,
        (fx["tx"].directory / "sealed-pr-package.json").read_bytes(),
        (fx["tx"].directory / "sealed-pr-body.md").read_bytes(),
    )
    assert not result.ok
    assert result.reason == pb.PublicationReason.APPROVAL_BINDING_INVALID


def test_diff_tree_parser_rejects_adversarial_raw_grammar():
    zero = "0" * 40
    old = "1" * 40
    new = "2" * 40
    valid = f":100644 100644 {old} {new} M\0a.txt\0"
    assert pb._parse_diff_tree_raw_z(valid.encode(), "sha1")[0]["status"] == "M"

    malformed = [
        valid[:-1],
        f":100644 100644 {old} {new} C100\0a\0b\0",
        f":100644 100644 {old} {new} R١٠٠\0a\0b\0",
        f":100644 100644 {old} {new} R101\0a\0b\0",
        f":000000 100644 {old} {new} A\0a\0",
        f":100644 000000 {old} {new} D\0a\0",
        f":100644 100755 {old} {new} M\0a\0",
        f":100644 100644 {old} {new} T\0a\0",
        f":000000 100644 {zero} {new} A\0../escape\0",
        (f":000000 100644 {zero} {new} A\0same\0:100644 100644 {old} {new} M\0same\0"),
    ]
    for raw in malformed:
        with pytest.raises(pb.TransactionError):
            pb._parse_diff_tree_raw_z(raw, "sha1")
    with pytest.raises(pb.TransactionError):
        pb._parse_diff_tree_raw_z(b"\xff\0", "sha1")
    with pytest.raises(pb.TransactionError):
        pb._parse_diff_tree_raw_z(valid, "sha256")
    with pytest.raises(pb.TransactionError):
        pb._parse_diff_tree_raw_z(object(), "sha1")


def test_raw_commit_parser_rejects_extra_reordered_or_malformed_headers():
    tree = "1" * 40
    parent = "2" * 40
    author = "A <a@example.test> 1700000000 +0000"
    committer = "C <c@example.test> 1700000000 +0000"
    message = b"approved message\n"
    headers = [
        f"tree {tree}",
        f"parent {parent}",
        f"author {author}",
        f"committer {committer}",
    ]
    valid = "\n".join(headers).encode() + b"\n\n" + message
    pb._parse_commit_object(
        valid,
        object_format="sha1",
        tree_oid=tree,
        parent_oid=parent,
        author_ident=author,
        committer_ident=committer,
        message=message,
    )
    bad_values = [
        "\n".join(headers + [f"parent {'3' * 40}"]).encode() + b"\n\n" + message,
        "\n".join([headers[1], headers[0], *headers[2:]]).encode() + b"\n\n" + message,
        "\n".join(headers + ["gpgsig injected"]).encode() + b"\n\n" + message,
        valid + b"\n",
        b"tree \xff\n\n" + message,
    ]
    for raw in bad_values:
        with pytest.raises(pb.TransactionError):
            pb._parse_commit_object(
                raw,
                object_format="sha1",
                tree_oid=tree,
                parent_oid=parent,
                author_ident=author,
                committer_ident=committer,
                message=message,
            )


@pytest.mark.parametrize(
    "surface", ["identity", "local", "gh_base", "remote", "pr", "sealed"]
)
def test_each_mutation_between_observation_passes_maps_to_pre_side_effect_failure(
    tmp_path, monkeypatch, surface
):
    fx = _publish_fixture(tmp_path, monkeypatch)
    counts = {surface: 0}

    if surface in {"identity", "local"}:
        original = pb.discover_repository

        def changed_identity(root):
            counts[surface] += 1
            observed = original(root)
            if counts[surface] == 2:
                if surface == "identity":
                    raise pb.RepositoryIdentityError("identity replaced between passes")
                return pb.RepositoryIdentity(
                    root=observed.root,
                    git_dir=observed.git_dir,
                    common_dir=observed.common_dir,
                    worktree_id=observed.worktree_id,
                    object_format=observed.object_format,
                    branch_ref=observed.branch_ref,
                    baseline_commit="f" * len(observed.baseline_commit),
                )
            return observed

        monkeypatch.setattr(pb, "discover_repository", changed_identity)
    elif surface == "gh_base":
        original = pb.subprocess.run

        def changed_gh(args, *a, **kw):
            if args[:3] == ["gh", "repo", "view"]:
                counts[surface] += 1
                if counts[surface] == 2:
                    return _FakeCompleted(
                        0,
                        json.dumps(
                            {
                                "nameWithOwner": "octo/demo",
                                "defaultBranchRef": {"name": "mutated-base"},
                            }
                        ),
                    )
            return original(args, *a, **kw)

        monkeypatch.setattr(pb.subprocess, "run", changed_gh)
    elif surface == "remote":
        original = pb._ls_remote_oid

        def changed_remote(root, branch_ref):
            counts[surface] += 1
            if counts[surface] == 2:
                return "f" * len(fx["payload"]["commit_oid"])
            return original(root, branch_ref)

        monkeypatch.setattr(pb, "_ls_remote_oid", changed_remote)
    elif surface == "pr":
        original = pb._classify_pr

        def changed_pr(*args, **kwargs):
            counts[surface] += 1
            if counts[surface] == 2:
                return (
                    "conflict",
                    {
                        "number": 7,
                        "url": "https://github.com/octo/demo/pull/7",
                    },
                )
            return original(*args, **kwargs)

        monkeypatch.setattr(pb, "_classify_pr", changed_pr)
    else:
        original = pb.validate_sealed_transaction
        body = fx["tx"].directory / "sealed-pr-body.md"

        def changed_seal(*args, **kwargs):
            counts[surface] += 1
            result = original(*args, **kwargs)
            if counts[surface] == 1:
                os.chmod(body, 0o600)
                body.write_bytes(b"changed between passes\n")
                os.chmod(body, 0o400)
            return result

        monkeypatch.setattr(pb, "validate_sealed_transaction", changed_seal)

    result = pb.publish_sealed_transaction(
        fx["identity"], fx["artifacts_dir"], fx["manifest"]
    )
    assert not result.ok
    assert result.reason == pb.PublicationReason.PRE_SIDE_EFFECT_REVALIDATION_FAILED
    assert counts[surface] >= 2
    assert fx["fake_gh"].create_calls == []
    assert not (fx["artifacts_dir"] / "publish.json").exists()
    state = json.loads((fx["artifacts_dir"] / "publication-state.json").read_bytes())
    assert state["last_reason"] == "pre_side_effect_revalidation_failed"
    if surface == "pr":
        assert state["pr_kind"] == "conflict"
        assert state["pr_url"].endswith("/pull/7")


def test_pr_classifier_exact_body_only_conflict_and_malformed_are_disjoint(
    tmp_path, monkeypatch
):
    fx = _publish_fixture(tmp_path, monkeypatch)
    payload = fx["payload"]
    body = b"PR body approved\n"
    exact = {
        "number": 3,
        "url": "https://github.com/octo/demo/pull/3",
        "headRefName": "feature",
        "headRefOid": payload["commit_oid"],
        "headRepositoryOwner": {"login": "octo"},
        "headRepository": {"name": "demo"},
        "baseRefName": "main",
        "title": payload["title"],
        "body": body.decode(),
    }
    observed = {"rows": [exact]}
    monkeypatch.setattr(pb, "_gh_json", lambda _args, _cwd: (True, observed["rows"]))
    assert pb._classify_pr(fx["identity"], payload, "octo/demo", body)[0] == "exact"

    body_bad = {**exact, "body": "different body"}
    observed["rows"] = [body_bad]
    assert (
        pb._classify_pr(fx["identity"], payload, "octo/demo", body)[0]
        == "body_mismatch"
    )

    mutations = [
        {**exact, "url": "https://github.com/other/demo/pull/3"},
        {**exact, "headRefName": "other"},
        {**exact, "headRefOid": "f" * len(payload["commit_oid"])},
        {**exact, "headRepositoryOwner": {"login": "other"}},
        {**exact, "headRepository": {"name": "other"}},
        {**exact, "baseRefName": "other"},
        {**exact, "title": "other"},
    ]
    for candidate in mutations:
        observed["rows"] = [candidate]
        assert (
            pb._classify_pr(fx["identity"], payload, "octo/demo", body)[0] == "conflict"
        )

    malformed = [
        {**exact, "number": True},
        {**exact, "extra": "field"},
        {**exact, "body": "\ud800"},
    ]
    for candidate in malformed:
        observed["rows"] = [candidate]
        assert (
            pb._classify_pr(fx["identity"], payload, "octo/demo", body)[0]
            == "ambiguous"
        )
    observed["rows"] = [
        exact,
        {**exact, "number": 4, "url": exact["url"].replace("3", "4")},
    ]
    assert pb._classify_pr(fx["identity"], payload, "octo/demo", body)[0] == "ambiguous"


@pytest.mark.parametrize("mode", ["lease_corrected_retry", "ambiguous_but_converged"])
def test_push_ambiguous_reconciliation_is_bounded_and_lease_correct(
    mode, tmp_path, monkeypatch
):
    fx = _publish_fixture(tmp_path, monkeypatch)
    original = pb._push_with_lease
    leases = []

    def controlled_push(root, expected_commit, branch_ref, lease_value):
        leases.append(lease_value)
        if len(leases) == 1 and mode == "lease_corrected_retry":
            _git(
                fx["main_root"],
                "push",
                "-q",
                "origin",
                f"{fx['payload']['baseline_commit']}:{branch_ref}",
            )
            raise subprocess.TimeoutExpired("git push", 1)
        result = original(root, expected_commit, branch_ref, lease_value)
        if len(leases) == 1 and mode == "ambiguous_but_converged":
            return subprocess.CompletedProcess(
                result.args,
                1,
                stdout=result.stdout,
                stderr=b"transport closed after update",
            )
        return result

    monkeypatch.setattr(pb, "_push_with_lease", controlled_push)
    result = pb.publish_sealed_transaction(
        fx["identity"], fx["artifacts_dir"], fx["manifest"]
    )
    assert result.ok, result.detail
    if mode == "lease_corrected_retry":
        assert leases == [None, fx["payload"]["baseline_commit"]]
    else:
        assert leases == [None]


@pytest.mark.parametrize(
    "mode", ["failed_then_retry", "exception_then_retry", "success_without_pr"]
)
def test_pr_create_retry_outcomes_are_exact_and_bounded(mode, tmp_path, monkeypatch):
    fx = _publish_fixture(tmp_path, monkeypatch)
    fake = fx["fake_gh"]
    if mode == "failed_then_retry":
        fake.fail_first_create = True
    else:
        original = FakeGitHub.__call__
        seen = {"creates": 0}

        def controlled_create(self, args, **kwargs):
            if args[1:3] == ["pr", "create"]:
                seen["creates"] += 1
                if mode == "exception_then_retry" and seen["creates"] == 1:
                    self.create_calls.append(
                        {
                            "args": list(args),
                            "input": kwargs.get("input"),
                            "shell": kwargs.get("shell"),
                        }
                    )
                    raise OSError("transport interrupted")
                if mode == "success_without_pr":
                    result = original(self, args, **kwargs)
                    self.prs.clear()
                    return result
            return original(self, args, **kwargs)

        monkeypatch.setattr(FakeGitHub, "__call__", controlled_create)

    result = pb.publish_sealed_transaction(
        fx["identity"], fx["artifacts_dir"], fx["manifest"]
    )
    if mode == "success_without_pr":
        assert not result.ok
        assert result.reason == pb.PublicationReason.PR_CREATE_FAILED
        assert len(fake.create_calls) == 1
    else:
        assert result.ok, result.detail
        assert len(fake.create_calls) == 2


def test_final_pr_reconciliation_blocks_post_create_identity_mutation(
    tmp_path, monkeypatch
):
    fx = _publish_fixture(tmp_path, monkeypatch)
    original = pb._classify_pr
    calls = {"count": 0}

    def mutate_only_final(*args, **kwargs):
        calls["count"] += 1
        kind, pr = original(*args, **kwargs)
        if calls["count"] == 5 and kind == "exact":
            changed = dict(pr)
            changed["title"] = "mutated after create reconciliation"
            return "conflict", changed
        return kind, pr

    monkeypatch.setattr(pb, "_classify_pr", mutate_only_final)
    result = pb.publish_sealed_transaction(
        fx["identity"], fx["artifacts_dir"], fx["manifest"]
    )
    assert not result.ok
    assert result.reason == pb.PublicationReason.REMOTE_PR_CONFLICT
    assert calls["count"] == 5
    state = json.loads((fx["artifacts_dir"] / "publication-state.json").read_bytes())
    assert state["pr_kind"] == "conflict"
    assert state["pr_url"].endswith("/pull/1")
    assert state["last_reason"] == "remote_pr_conflict"


def test_transaction_layout_counts_and_loose_object_rehash_are_exact(
    tmp_path, monkeypatch
):
    fx = _publish_fixture(tmp_path, monkeypatch)
    sealed = fx["tx"].directory
    assert {p.name for p in sealed.iterdir()} == {
        "index",
        "objects",
        "sealed-pr-package.json",
        "sealed-pr-body.md",
        "transaction.json",
    }
    assert not (sealed / "objects" / "info" / "alternates").exists()
    object_files = [p for fan in (sealed / "objects").iterdir() for p in fan.iterdir()]
    metadata_bytes = (sealed / "transaction.json").read_bytes()
    metadata = json.loads(metadata_bytes)
    assert metadata_bytes == pb.canonical_json(metadata)
    assert metadata["object_count"] == len(object_files)
    assert fx["manifest"]["sealed_transaction"]["object_count"] == len(object_files)

    victim = object_files[0]
    os.chmod(victim, 0o600)
    victim.write_bytes(victim.read_bytes() + b"trailing-junk")
    os.chmod(victim, 0o400)
    result = pb.validate_sealed_transaction(
        fx["identity"],
        fx["artifacts_dir"],
        pb.canonical_json(fx["manifest"]),
        (sealed / "sealed-pr-package.json").read_bytes(),
        (sealed / "sealed-pr-body.md").read_bytes(),
    )
    assert not result.ok
    assert "zlib stream" in result.detail


def test_object_install_is_exclusive_idempotent_and_reconciles_concurrent_winner(
    tmp_path, monkeypatch
):
    fx = _publish_fixture(tmp_path, monkeypatch)
    sealed = fx["tx"].directory
    total = fx["manifest"]["sealed_transaction"]["object_count"]

    common = tmp_path / "install-common"
    (common / "objects").mkdir(parents=True)
    assert (
        pb._install_sealed_objects(sealed, common, fx["identity"].object_format)
        == total
    )
    assert pb._install_sealed_objects(sealed, common, fx["identity"].object_format) == 0

    raced_common = tmp_path / "raced-common"
    (raced_common / "objects").mkdir(parents=True)
    original = pb.atomic_write
    raced = {"done": False}

    def concurrent_winner(path, data, **kwargs):
        if path.is_relative_to(raced_common) and not raced["done"]:
            raced["done"] = True
            original(path, data, **kwargs)
            raise FileExistsError("concurrent winner")
        return original(path, data, **kwargs)

    monkeypatch.setattr(pb, "atomic_write", concurrent_winner)
    installed = pb._install_sealed_objects(
        sealed, raced_common, fx["identity"].object_format
    )
    assert raced["done"] and installed == total - 1

    bad_common = tmp_path / "bad-common"
    (bad_common / "objects").mkdir(parents=True)

    def concurrent_collision(path, data, **kwargs):
        if path.is_relative_to(bad_common):
            original(path, b"not-a-loose-object", **kwargs)
            raise FileExistsError("concurrent collision")
        return original(path, data, **kwargs)

    monkeypatch.setattr(pb, "atomic_write", concurrent_collision)
    with pytest.raises(pb.TransactionError, match="invalid loose object"):
        pb._install_sealed_objects(sealed, bad_common, fx["identity"].object_format)


def test_confined_io_detects_metadata_parent_and_no_overwrite_races(
    tmp_path, monkeypatch
):
    source = tmp_path / "source.bin"
    source.write_bytes(b"stable bytes")
    original_read = pb.os.read
    touched = {"done": False}

    def mutate_while_open(fd, size):
        data = original_read(fd, size)
        if data and not touched["done"]:
            touched["done"] = True
            st = source.stat()
            os.utime(source, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
        return data

    with monkeypatch.context() as m:
        m.setattr(pb.os, "read", mutate_while_open)
        with pytest.raises(OSError, match="identity/metadata"):
            pb.read_confined_regular_bytes(source)

    dest = tmp_path / "exclusive.bin"
    original_link = pb.os.link

    def competing_link(src, dst, **kwargs):
        Path(dst).write_bytes(b"concurrent data")
        return original_link(src, dst, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(pb.os, "link", competing_link)
        with pytest.raises(FileExistsError):
            pb.atomic_write(dest, b"approved", overwrite=False)
    assert dest.read_bytes() == b"concurrent data"

    with monkeypatch.context() as m:
        m.setattr(pb, "_is_reparse_stat", lambda st: True)
        with pytest.raises(OSError, match="reparse"):
            pb.read_confined_regular_bytes(source)

    parent_calls = {"count": 0}
    original_lstat = pb.os.lstat

    def replaced_parent(path):
        observed = original_lstat(path)
        if Path(path) == tmp_path:
            parent_calls["count"] += 1
            if parent_calls["count"] == 3:
                values = list(observed)
                values[1] += 1
                return os.stat_result(values)
        return observed

    with monkeypatch.context() as m:
        m.setattr(pb.os, "lstat", replaced_parent)
        with pytest.raises(OSError, match="parent directory identity"):
            pb.atomic_write(tmp_path / "parent-race.bin", b"approved")

    if hasattr(pb.os, "O_NOFOLLOW"):
        flags_seen = []
        original_open = pb.os.open

        def capture_open(path, flags, *args, **kwargs):
            flags_seen.append(flags)
            return original_open(path, flags, *args, **kwargs)

        with monkeypatch.context() as m:
            m.setattr(pb.os, "open", capture_open)
            pb.atomic_write(tmp_path / "nofollow.bin", b"approved")
            pb.read_confined_regular_bytes(tmp_path / "nofollow.bin")
        assert sum(bool(flags & pb.os.O_NOFOLLOW) for flags in flags_seen) >= 2


def test_lock_record_layout_and_stale_owner_proof(tmp_path, monkeypatch):
    artifacts = tmp_path / "artifacts"
    common = tmp_path / "repo" / ".git"
    common.mkdir(parents=True)
    branch = "refs/heads/main"
    lock_path = artifacts / "locks" / f"{pb._lock_name(common, branch)}.lock"
    writes = []
    original_write = pb.os.write

    def capture_write(fd, data):
        if data.startswith(b"{"):
            writes.append(data)
        return original_write(fd, data)

    monkeypatch.setattr(pb.os, "write", capture_write)
    with pb.acquire_repository_lock(artifacts, common, branch, run_id="layout"):
        record = json.loads(writes[-1])
        assert set(record) == {
            "schema",
            "pid",
            "process_start_marker",
            "hostname",
            "run_id",
            "created_at",
        }
        assert record["process_start_marker"]

    live_record = {
        "schema": 1,
        "pid": os.getpid(),
        "process_start_marker": pb._PROCESS_START_MARKER,
        "hostname": pb.socket.gethostname(),
        "run_id": "stale-live",
        "created_at": 1.0,
    }
    lock_path.write_text(json.dumps(live_record), encoding="utf-8")
    with pytest.raises(pb.PublicationLockedError):
        with pb.acquire_repository_lock(artifacts, common, branch, run_id="blocked"):
            pass
    assert json.loads(lock_path.read_text(encoding="utf-8"))["run_id"] == "stale-live"


def test_strict_diff_tree_raw_parser_rejects_every_non_normative_shape():
    zero = "0" * 40
    oid = "a" * 40
    valid = f":000000 100644 {zero} {oid} A\0a.txt\0"
    parsed = pb._parse_diff_tree_raw_z(valid, "sha1")
    assert parsed[0]["status"] == "A"
    assert parsed[0]["new_path"] == "a.txt"

    invalid = [
        valid[:-1],
        f":000000 100644 {zero} {oid.upper()} A\0a.txt\0",
        f":000000 100644 {zero} {oid} C100\0old\0new\0",
        f":000000 100644 {zero} {oid} A001\0a.txt\0",
        f":000000 100644 {zero} {oid} A\0../escape\0",
        f":000000 100644 {zero} {oid} A\0a.txt\0:000000 100644 {zero} {oid} A\0a.txt\0",
        f":000000 100664 {zero} {oid} A\0a.txt\0",
        valid + "\0",
    ]
    for raw in invalid:
        with pytest.raises(pb.TransactionError):
            pb._parse_diff_tree_raw_z(raw, "sha1")


def test_raw_commit_parser_requires_exact_headers_order_parent_and_message_bytes():
    tree = "a" * 40
    parent = "b" * 40
    author = "A <a@example.test> 1700000000 +0000"
    committer = "C <c@example.test> 1700000000 +0000"
    message = b"approved message\n"
    headers = (
        f"tree {tree}\nparent {parent}\nauthor {author}\ncommitter {committer}"
    ).encode()
    exact = headers + b"\n\n" + message
    pb._parse_commit_object(
        exact,
        object_format="sha1",
        tree_oid=tree,
        parent_oid=parent,
        author_ident=author,
        committer_ident=committer,
        message=message,
    )

    variants = [
        exact.replace(b"tree ", b"Tree ", 1),
        exact.replace(b"parent ", b"parent " + (b"c" * 40) + b"\nparent ", 1),
        exact.replace(b"author ", b"encoding UTF-8\nauthor ", 1),
        exact.replace(b"\n", b"\r\n", 1),
        exact[:-1],
        exact + b"extra",
    ]
    for raw in variants:
        with pytest.raises(pb.TransactionError):
            pb._parse_commit_object(
                raw,
                object_format="sha1",
                tree_oid=tree,
                parent_oid=parent,
                author_ident=author,
                committer_ident=committer,
                message=message,
            )


def test_strict_pr_shape_binds_repo_owner_name_ref_sha_base_title_and_body(
    tmp_path, monkeypatch
):
    fx = _publish_fixture(tmp_path, monkeypatch)
    payload = fx["payload"]
    identity = fx["identity"]
    slug = fx["fake_gh"].repo_slug
    branch = identity.branch_ref.removeprefix("refs/heads/")
    body = b"PR body approved\n"
    exact = {
        "number": 7,
        "url": f"https://github.com/{slug}/pull/7",
        "headRefName": branch,
        "headRefOid": payload["commit_oid"],
        "headRepositoryOwner": {"login": "octo"},
        "headRepository": {"name": "demo"},
        "baseRefName": payload["pr_base_branch"],
        "title": payload["title"],
        "body": body.decode(),
    }

    observed = exact
    monkeypatch.setattr(pb, "_gh_json", lambda _args, _cwd: (True, [observed]))
    assert pb._classify_pr(identity, payload, slug, body)[0] == "exact"

    conflicts = [
        {**exact, "url": "https://github.com/other/demo/pull/7"},
        {**exact, "headRefName": "other"},
        {**exact, "headRefOid": payload["baseline_commit"]},
        {**exact, "baseRefName": "other"},
        {**exact, "title": "other"},
        {**exact, "headRepositoryOwner": {"login": "other"}},
        {**exact, "headRepository": {"name": "other"}},
    ]
    for observed in conflicts:
        assert pb._classify_pr(identity, payload, slug, body)[0] == "conflict"

    observed = {**exact, "body": "body-only mismatch"}
    assert pb._classify_pr(identity, payload, slug, body)[0] == "body_mismatch"

    malformed = [
        {**exact, "body": 1},
        {**exact, "headRepositoryOwner": {"login": 1}},
        {**exact, "extra": "field"},
        {key: value for key, value in exact.items() if key != "headRepository"},
    ]
    for observed in malformed:
        assert pb._classify_pr(identity, payload, slug, body)[0] == "ambiguous"


def test_ordered_fresh_observation_runs_twice_immediately_before_install(
    tmp_path, monkeypatch
):
    fx = _publish_fixture(tmp_path, monkeypatch)
    events: list[str] = []
    real_discover = pb.discover_repository
    real_validate = pb.validate_sealed_transaction
    real_ls_remote = pb._ls_remote_oid
    real_gh_json = pb._gh_json
    real_install = pb._install_sealed_objects

    def discover(root):
        events.append("discover")
        return real_discover(root)

    def validate(*args, **kwargs):
        events.append("sealed")
        return real_validate(*args, **kwargs)

    def ls_remote(*args, **kwargs):
        events.append("remote")
        return real_ls_remote(*args, **kwargs)

    def gh_json(args, cwd):
        events.append("repo" if args[:2] == ["repo", "view"] else "pr")
        return real_gh_json(args, cwd)

    def install(*args, **kwargs):
        events.append("install")
        return real_install(*args, **kwargs)

    monkeypatch.setattr(pb, "discover_repository", discover)
    monkeypatch.setattr(pb, "validate_sealed_transaction", validate)
    monkeypatch.setattr(pb, "_ls_remote_oid", ls_remote)
    monkeypatch.setattr(pb, "_gh_json", gh_json)
    monkeypatch.setattr(pb, "_install_sealed_objects", install)

    result = pb.publish_sealed_transaction(
        fx["identity"], fx["artifacts_dir"], fx["manifest"]
    )
    assert result.ok, result.detail
    before_install = events[: events.index("install")]
    assert before_install == [
        "discover",
        "repo",
        "remote",
        "sealed",
        "pr",
        "discover",
        "repo",
        "remote",
        "sealed",
        "pr",
    ]


def test_ambiguous_push_persists_state_and_gets_one_matching_lease_retry(
    tmp_path, monkeypatch
):
    fx = _publish_fixture(tmp_path, monkeypatch)
    real_push = pb._push_with_lease
    real_write = pb._write_publication_state
    pushes: list[str | None] = []
    states: list[dict] = []

    def push(root, commit, branch_ref, lease_value):
        pushes.append(lease_value)
        if len(pushes) == 1:
            return _FakeCompleted(1, "", "simulated transport failure")
        return real_push(root, commit, branch_ref, lease_value)

    def write_state(artifacts_dir, state):
        states.append(dict(state))
        return real_write(artifacts_dir, state)

    monkeypatch.setattr(pb, "_push_with_lease", push)
    monkeypatch.setattr(pb, "_write_publication_state", write_state)
    result = pb.publish_sealed_transaction(
        fx["identity"], fx["artifacts_dir"], fx["manifest"]
    )
    assert result.ok, result.detail
    assert pushes == [None, None]
    assert any(
        state["state"] == "push_outcome_ambiguous"
        and state["last_reason"] == "push_outcome_ambiguous"
        for state in states
    )


def test_ambiguous_pr_create_retries_once_with_same_snapshot_and_final_requery(
    tmp_path, monkeypatch
):
    fx = _publish_fixture(tmp_path, monkeypatch)
    prior_run = pb.subprocess.run
    create_inputs: list[bytes] = []
    create_timeouts: list[float] = []
    classify_results: list[str] = []
    real_classify = pb._classify_pr

    def timeout_once(args, *positional, **kwargs):
        if args[:3] == ["gh", "pr", "create"]:
            create_inputs.append(kwargs["input"])
            create_timeouts.append(kwargs["timeout"])
            if len(create_inputs) == 1:
                raise subprocess.TimeoutExpired(args, 1)
        return prior_run(args, *positional, **kwargs)

    def classify(*args, **kwargs):
        result = real_classify(*args, **kwargs)
        classify_results.append(result[0])
        return result

    monkeypatch.setattr(pb.subprocess, "run", timeout_once)
    monkeypatch.setattr(pb, "_classify_pr", classify)
    result = pb.publish_sealed_transaction(
        fx["identity"], fx["artifacts_dir"], fx["manifest"]
    )
    assert result.ok, result.detail
    assert create_inputs == [b"PR body approved\n", b"PR body approved\n"]
    assert create_timeouts == [
        pb._NETWORK_TIMEOUT_SECONDS,
        pb._NETWORK_TIMEOUT_SECONDS,
    ]
    assert classify_results[-2:] == ["exact", "exact"]
    assert len(fx["fake_gh"].list_calls) >= 5


def test_nominal_pr_create_success_without_qualified_pr_is_not_retried(
    tmp_path, monkeypatch
):
    fx = _publish_fixture(tmp_path, monkeypatch)
    prior_run = pb.subprocess.run
    create_inputs: list[bytes] = []

    def success_without_pr(args, *positional, **kwargs):
        if args[:3] == ["gh", "pr", "create"]:
            create_inputs.append(kwargs["input"])
            return _FakeCompleted(0, "https://github.com/untrusted/output/pull/1")
        return prior_run(args, *positional, **kwargs)

    monkeypatch.setattr(pb.subprocess, "run", success_without_pr)
    result = pb.publish_sealed_transaction(
        fx["identity"], fx["artifacts_dir"], fx["manifest"]
    )
    assert not result.ok
    assert result.reason == pb.PublicationReason.PR_CREATE_FAILED
    assert create_inputs == [b"PR body approved\n"]
    state = json.loads((fx["artifacts_dir"] / "publication-state.json").read_bytes())
    assert state["last_reason"] == pb.PublicationReason.PR_CREATE_FAILED


def test_build_sealed_transaction_public_signature_is_exact():
    assert list(inspect.signature(pb.build_sealed_transaction).parameters) == [
        "identity",
        "artifacts_dir",
        "package_bytes",
        "body_bytes",
        "allowed_paths",
    ]


def test_publish_captures_manifest_path_once_and_reuses_exact_snapshot(
    tmp_path, monkeypatch
):
    fx = _publish_fixture(tmp_path, monkeypatch)
    real_read = pb.read_confined_regular_bytes
    manifest_reads = []

    def counted_read(path, **kwargs):
        if Path(path) == fx["artifacts_dir"] / "approval-manifest.json":
            manifest_reads.append(Path(path))
        return real_read(path, **kwargs)

    monkeypatch.setattr(pb, "read_confined_regular_bytes", counted_read)
    result = pb.publish_sealed_transaction(fx["identity"], fx["artifacts_dir"])
    assert result.ok, result.detail
    assert manifest_reads == [fx["artifacts_dir"] / "approval-manifest.json"]


@pytest.mark.parametrize(
    "raw_manifest",
    [b'{"schema":2,"schema":2}', None],
    ids=["duplicate-key", "noncanonical-trailing-lf"],
)
def test_publish_rejects_duplicate_or_noncanonical_manifest_bytes_before_effects(
    raw_manifest, tmp_path, monkeypatch
):
    fx = _publish_fixture(tmp_path, monkeypatch)
    if raw_manifest is None:
        raw_manifest = pb.canonical_json(fx["manifest"]) + b"\n"
    (fx["artifacts_dir"] / "approval-manifest.json").write_bytes(raw_manifest)

    result = pb.publish_sealed_transaction(fx["identity"], fx["artifacts_dir"])
    assert not result.ok
    assert result.reason == pb.PublicationReason.APPROVAL_BINDING_INVALID
    assert fx["fake_gh"].create_calls == []
    assert not (fx["artifacts_dir"] / "publish.json").exists()


@pytest.mark.parametrize("entry_name", ["index", "objects"])
def test_sealed_git_paths_reject_symlink_or_reparse_substitution(
    entry_name, tmp_path, monkeypatch
):
    fx = _publish_fixture(tmp_path, monkeypatch)
    sealed_dir = fx["tx"].directory
    os.chmod(sealed_dir, 0o700)
    entry = sealed_dir / entry_name
    outside = tmp_path / f"outside-{entry_name}"
    entry.rename(outside)
    try:
        entry.symlink_to(outside, target_is_directory=entry_name == "objects")
    except OSError:
        pytest.skip("symlink creation not permitted in this environment")

    result = pb.publish_sealed_transaction(fx["identity"], fx["artifacts_dir"])
    assert not result.ok
    assert result.reason == pb.PublicationReason.SEALED_TRANSACTION_INVALID
    assert fx["fake_gh"].create_calls == []


@pytest.mark.parametrize(
    ("surface", "expected_reason"),
    [
        ("repo", pb.PublicationReason.REPOSITORY_IDENTITY_MISMATCH),
        ("list", pb.PublicationReason.REMOTE_PR_AMBIGUOUS),
    ],
)
def test_gh_repo_and_pr_list_timeouts_are_finite_and_fail_closed(
    surface, expected_reason, tmp_path, monkeypatch
):
    fx = _publish_fixture(tmp_path, monkeypatch)
    prior_run = pb.subprocess.run
    observed_timeouts = []
    target = ["gh", "repo", "view"] if surface == "repo" else ["gh", "pr", "list"]

    def timeout_surface(args, *positional, **kwargs):
        if args[:3] == target:
            observed_timeouts.append(kwargs.get("timeout"))
            raise subprocess.TimeoutExpired(args, kwargs["timeout"])
        return prior_run(args, *positional, **kwargs)

    monkeypatch.setattr(pb.subprocess, "run", timeout_surface)
    result = pb.publish_sealed_transaction(fx["identity"], fx["artifacts_dir"])
    assert not result.ok
    assert result.reason == expected_reason
    assert observed_timeouts == [pb._NETWORK_TIMEOUT_SECONDS]
    assert fx["fake_gh"].create_calls == []


def test_push_timeout_has_finite_deadline_and_one_reconciled_retry(
    tmp_path, monkeypatch
):
    fx = _publish_fixture(tmp_path, monkeypatch)
    prior_run = pb.subprocess.run
    push_timeouts = []

    def timeout_first_push(args, *positional, **kwargs):
        if args[:2] == ["git", "push"]:
            push_timeouts.append(kwargs.get("timeout"))
            if len(push_timeouts) == 1:
                raise subprocess.TimeoutExpired(args, kwargs["timeout"])
        return prior_run(args, *positional, **kwargs)

    monkeypatch.setattr(pb.subprocess, "run", timeout_first_push)
    result = pb.publish_sealed_transaction(fx["identity"], fx["artifacts_dir"])
    assert result.ok, result.detail
    assert push_timeouts == [
        pb._NETWORK_TIMEOUT_SECONDS,
        pb._NETWORK_TIMEOUT_SECONDS,
    ]


def test_ls_remote_timeout_has_finite_deadline_and_stable_reason(tmp_path, monkeypatch):
    observed = []

    def timeout_ls_remote(args, **kwargs):
        assert args[:2] == ["git", "ls-remote"]
        observed.append(kwargs.get("timeout"))
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(pb.subprocess, "run", timeout_ls_remote)
    with pytest.raises(pb.TransactionError) as excinfo:
        pb._ls_remote_oid(tmp_path, "refs/heads/feature")
    assert excinfo.value.reason == "remote_verification_failed"
    assert observed == [pb._NETWORK_TIMEOUT_SECONDS]
