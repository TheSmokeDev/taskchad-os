from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT_PATH = Path(__file__).resolve().parent / "sync_seo_stack_skills.py"
SPEC = importlib.util.spec_from_file_location("sync_seo_stack_skills", SCRIPT_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class SkillSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source_root = self.root / "source"
        self.target_root = self.root / "target"
        self.skill = MODULE.SKILLS[0]
        package = self.source_root / self.skill
        (package / "scripts").mkdir(parents=True)
        (package / "SKILL.md").write_text("---\nname: fixture\n---\n", encoding="utf-8")
        (package / "scripts" / "tool.py").write_text("VALUE = 1\n", encoding="utf-8")
        cache = package / "scripts" / "__pycache__"
        cache.mkdir()
        (cache / "tool.pyc").write_bytes(b"ignored")
        self.source_patch = patch.object(MODULE, "SOURCE_ROOT", self.source_root)
        self.source_patch.start()

    def tearDown(self) -> None:
        self.source_patch.stop()
        self.temporary.cleanup()

    def test_install_is_hash_identical_and_excludes_cache(self) -> None:
        target = MODULE.install_skill(self.skill, self.target_root)
        self.assertTrue(MODULE.compare_packages(self.source_root / self.skill, target).matches)
        self.assertFalse((target / "scripts" / "__pycache__").exists())

    def test_install_removes_stale_files(self) -> None:
        target = self.target_root / self.skill
        target.mkdir(parents=True)
        (target / "stale.txt").write_text("stale", encoding="utf-8")

        MODULE.install_skill(self.skill, self.target_root)

        self.assertFalse((target / "stale.txt").exists())

    def test_check_reports_changed_and_extra_files(self) -> None:
        target = MODULE.install_skill(self.skill, self.target_root)
        (target / "SKILL.md").write_text("changed", encoding="utf-8")
        (target / "extra.txt").write_text("extra", encoding="utf-8")

        comparison = MODULE.compare_packages(self.source_root / self.skill, target)

        self.assertEqual(comparison.changed, ("SKILL.md",))
        self.assertEqual(comparison.extra, ("extra.txt",))

    def test_unknown_skill_and_source_target_are_rejected(self) -> None:
        with self.assertRaises(MODULE.SyncError):
            MODULE.target_directory(self.target_root, "../escape")
        with self.assertRaises(MODULE.SyncError):
            MODULE.target_directory(self.source_root, self.skill)

    def test_cleanup_rejects_path_outside_target_root(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()

        with self.assertRaises(MODULE.SyncError):
            MODULE._remove_tree(outside, self.target_root)

        self.assertTrue(outside.is_dir())

    def test_cli_check_returns_nonzero_for_missing_install(self) -> None:
        result = MODULE.main(["check", "--target-root", str(self.target_root), "--skill", self.skill])
        self.assertEqual(result, 1)


if __name__ == "__main__":
    unittest.main()
