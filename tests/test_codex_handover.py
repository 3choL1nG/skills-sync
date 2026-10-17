"""Verify remaining explicit rework-02 requirements; synthetic data only."""
from pathlib import Path
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

DELIVERY = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(DELIVERY / "src"), str(DELIVERY / "tests")]
from harness import SyncTestCase
from skills_sync import util
from skills_sync.config import Config
from skills_sync.engine import Engine


def record(name, data):
    print("PROBE " + name + " " + json.dumps(data, ensure_ascii=False, default=str), flush=True)


class TransactionProbes(SyncTestCase):
    def shared(self):
        a, b = self.two_devices()
        a.write_skill("codex", "alpha", {"SKILL.md": "# Alpha\n\nbase\n"})
        self.allow_all(a)
        self.ok(a, "sync")
        self.ok(b, "sync")
        return a, b

    def test_bug04_edit_during_backup_is_kept_before_swap(self):
        a, b = self.shared()
        a.write_skill("codex", "alpha", {"SKILL.md": "# Alpha\n\nremote v2\n"})
        self.ok(a, "sync")
        e = Engine(Config.load(b.config))
        original = e.state.backup_skill
        injected = []
        def backup_then_external_edit(*args, **kwargs):
            result = original(*args, **kwargs)
            b.write_skill("codex", "alpha", {"SKILL.md": "# Alpha\n\nEDIT DURING BACKUP\n"})
            injected.append(True)
            return result
        e.state.backup_skill = backup_then_external_edit
        try:
            result = e.sync()
        except Exception as exc:
            result = {"status": "exception", "type": type(exc).__name__, "message": str(exc)}
        current = b.read("codex", "alpha", "SKILL.md")
        record("edit-during-backup", {"injected": bool(injected), "result": result, "current": current})
        self.assertTrue(injected)
        self.assertIn("EDIT DURING BACKUP", current)
        self.assertNotEqual(result.get("status"), "ok")

    def test_bug07_new_edit_during_restore_staging_is_kept(self):
        a, b = self.shared()
        op = self.ok(b, "uninstall", "alpha", "--yes")["data"]["op_id"]
        self.ok(b, "restore", "alpha")
        e = Engine(Config.load(b.config))
        backup = e.state.backup_dir(op, "alpha") / "files"
        original = util.copy_tree
        injected = []
        def copy_then_external_edit(src, dst):
            result = original(src, dst)
            if Path(src) == backup:
                b.write_skill("codex", "alpha", {"SKILL.md": "# Alpha\n\nEDIT DURING RECOVER\n"})
                injected.append(True)
            return result
        with patch.object(util, "copy_tree", side_effect=copy_then_external_edit):
            try:
                result = e.recover(restore=f"{op}/alpha")
            except Exception as exc:
                result = {"status": "exception", "type": type(exc).__name__, "message": str(exc)}
        current = b.read("codex", "alpha", "SKILL.md")
        record("edit-during-recover", {"injected": bool(injected), "result": result, "current": current})
        self.assertTrue(injected)
        self.assertIn("EDIT DURING RECOVER", current)
        self.assertNotEqual(result.get("status"), "ok")

    def test_bug08_target_intermediate_junction_never_written(self):
        src = self.world.base / "source"
        dst = self.world.base / "destination"
        outside = self.world.base / "outside-target"
        (src / "refs").mkdir(parents=True)
        dst.mkdir()
        outside.mkdir()
        (src / "refs" / "data.txt").write_text("SOURCE BYTES", encoding="utf-8")
        files = util.snapshot_dir(src)
        quote = lambda p: "'" + str(p).replace("'", "''") + "'"
        command = "New-Item -ItemType Junction -Path " + quote(dst / "refs") + " -Target " + quote(outside) + " | Out-Null"
        p = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command], capture_output=True, text=True)
        if p.returncode:
            self.skipTest("Cannot create isolated junction: " + p.stderr)
        try:
            util.copy_selected(src, files, dst)
            result = {"status": "copied"}
        except Exception as exc:
            result = {"status": "rejected", "type": type(exc).__name__, "message": str(exc)}
        written = outside / "data.txt"
        record("target-junction", {"result": result, "outside_exists": written.exists(),
                                   "outside_content": written.read_text() if written.exists() else None})
        self.assertFalse(written.exists(), "copy_selected wrote outside its destination through a parent junction")

    def test_bug04_delete_backup_window_keeps_new_edit(self):
        a, b = self.shared()
        self.ok(a, "delete-shared", "alpha", "--yes")
        e = Engine(Config.load(b.config))
        original = e.state.backup_skill
        def backup_then_edit(*args, **kwargs):
            result = original(*args, **kwargs)
            b.write_skill("codex", "alpha", {"SKILL.md": "# Alpha\n\nEDIT BEFORE DELETE\n"})
            return result
        e.state.backup_skill = backup_then_edit
        result = e.sync()
        self.assertIn("EDIT BEFORE DELETE", b.read("codex", "alpha", "SKILL.md"))
        self.assertNotEqual(result["status"], "ok")

    def test_bug04_detached_directory_is_checked_before_discard(self):
        a, _ = self.two_devices()
        e = Engine(Config.load(a.config))
        target = self.world.base / "replace-target"
        staged = self.world.base / "prepared"
        target.mkdir()
        staged.mkdir()
        (target / "data.txt").write_text("OLD", encoding="utf-8")
        (staged / "data.txt").write_text("REPLACEMENT", encoding="utf-8")
        expected = {"exists": True, "files": util.snapshot_dir(target)}
        original = os.replace
        def rename_with_edit(src, dst):
            if Path(src) == target and ".ss-old-" in Path(dst).name:
                (target / "data.txt").write_text("EDIT AT DETACH", encoding="utf-8")
            return original(src, dst)
        with patch.object(os, "replace", side_effect=rename_with_edit):
            with self.assertRaises(Exception) as caught:
                e._replace_dir(staged, target, expected=expected)
        self.assertEqual(type(caught.exception).__name__, "ConflictPending")
        self.assertEqual((target / "data.txt").read_text(), "EDIT AT DETACH")
        self.assertEqual((staged / "data.txt").read_text(), "REPLACEMENT")

    def test_bug07_corrupt_backup_is_not_used_for_restore(self):
        a, b = self.shared()
        op = self.ok(b, "uninstall", "alpha", "--yes")["data"]["op_id"]
        self.ok(b, "restore", "alpha")
        e = Engine(Config.load(b.config))
        (e.state.backup_dir(op, "alpha") / "files" / "SKILL.md").write_text("CORRUPTED", encoding="utf-8")
        with self.assertRaises(Exception) as caught:
            e.recover(restore=f"{op}/alpha")
        self.assertEqual(type(caught.exception).__name__, "StateError")
        self.assertEqual(b.read("codex", "alpha", "SKILL.md"), "# Alpha\n\nbase\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
