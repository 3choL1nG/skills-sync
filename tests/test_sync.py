# -*- coding: utf-8 -*-
"""AC-01/02/03/04/05/06/07：首次汇总、第三方资源、审批、两端合并与冲突。"""
from __future__ import annotations

import json
import subprocess
import unittest

from harness import GIT, SyncTestCase, git


class RoundtripTest(SyncTestCase):
    def test_first_import_and_deploy(self):
        """AC-01：空 bare 远端，A 汇总 → B 安装；再由 B 新建 → A 接收。"""
        a, b = self.two_devices()
        a.write_skill("codex", "alpha", self.skill_md("Alpha", "alpha v1"))
        rc, data = a.run("sync")
        self.assertEqual(rc, 3, data)
        self.assertIn("alpha", data["data"]["pending"])

        self.allow_all(a)
        data = self.ok(a, "sync")
        self.assertIn("alpha", data["data"]["pushed"])

        data = self.ok(b, "sync")
        self.assertIn("alpha", data["data"]["deployed"])
        self.assertEqual(b.read("codex", "alpha", "SKILL.md"), "# Alpha\n\nalpha v1\n")
        self.assertTrue((b.base / "state" / "state.json").exists())
        self.assertIn("alpha", b.state()["baselines"])

        # 反方向：B 新建 skill，A 接收
        b.write_skill("codex", "gamma", self.skill_md("Gamma", "gamma from B"))
        self.allow_all(b)
        data = self.ok(b, "sync")
        self.assertIn("gamma", data["data"]["pushed"])
        data = self.ok(a, "sync")
        self.assertIn("gamma", data["data"]["deployed"])
        self.assertEqual(a.read("codex", "gamma", "SKILL.md"), "# Gamma\n\ngamma from B\n")

    def test_third_party_assets_preserved(self):
        """AC-02：脚本/模板/二进制/许可证完整同步。"""
        a, b = self.two_devices()
        a.write_skill(
            "codex", "vendor-skill",
            {
                "SKILL.md": "# Vendor\n\nUse it.\n",
                "scripts/run.sh": "#!/bin/sh\necho run\n",
                "templates/page.html": "<h1>{{title}}</h1>\n",
                "LICENSE": "MIT License\n",
            },
            binary={"assets/logo.bin": b"\x00\x01\x02\xff"},
        )
        self.allow_all(a)
        self.ok(a, "sync")
        self.ok(b, "sync")
        root = b.roots["codex"] / "vendor-skill"
        self.assertEqual((root / "scripts" / "run.sh").read_text(encoding="utf-8"),
                         "#!/bin/sh\necho run\n")
        self.assertEqual((root / "assets" / "logo.bin").read_bytes(), b"\x00\x01\x02\xff")
        self.assertEqual((root / "LICENSE").read_text(encoding="utf-8"), "MIT License\n")

    def test_pending_skill_not_pushed_until_approved(self):
        """AC-03：未批准的新 skill 不推送；批准后才进入共享库。"""
        a, _ = self.two_devices()
        a.write_skill("codex", "draft", self.skill_md("Draft"))
        rc, data = a.run("sync")
        self.assertEqual(rc, 3)
        self.assertIn("draft", data["data"]["pending"])
        tree = git("ls-tree", "-r", "--name-only", "main", cwd=self.world.remote_root / "remote.git").stdout
        self.assertNotIn("skills/draft", tree)

        self.allow_all(a)
        self.ok(a, "sync")
        tree = git("ls-tree", "-r", "--name-only", "main", cwd=self.world.remote_root / "remote.git").stdout
        self.assertIn("skills/draft/SKILL.md", tree)

    def test_local_only_not_propagated(self):
        """AC-03 补充：仅本机 skill 不推送、不传播。"""
        a, b = self.two_devices()
        a.write_skill("codex", "private", self.skill_md("Private"))
        self.ok(a, "include", "private", "--exclude")
        self.ok(a, "sync")
        tree = git("ls-tree", "-r", "--name-only", "main", cwd=self.world.remote_root / "remote.git").stdout
        self.assertNotIn("private", tree)
        self.ok(b, "sync")
        self.assertFalse((b.roots["codex"] / "private").exists())


class MergeTest(SyncTestCase):
    def _two_copies(self):
        a, b = self.two_devices()
        a.write_skill("codex", "alpha", {
            "SKILL.md": "# Alpha\n\nline1\nline2\nline3\nline4\nline5\n",
            "run.py": "print('v1')\n",
        })
        self.allow_all(a)
        self.ok(a, "sync")
        self.ok(b, "sync")
        return a, b

    def test_different_files_merge(self):
        """AC-04：两端各改不同文件 → 自动合并。"""
        a, b = self._two_copies()
        a.write_skill("codex", "alpha", {"run.py": "print('v2 from A')\n"})
        self.ok(a, "sync")
        b.write_skill("codex", "alpha", {"SKILL.md": "# Alpha\n\nline1\nline2\nline3\nline4\nline5\nB tail\n"})
        data = self.ok(b, "sync")
        self.assertIn("alpha", data["data"]["merged"])
        self.assertEqual(b.read("codex", "alpha", "run.py"), "print('v2 from A')\n")
        self.assertIn("B tail", b.read("codex", "alpha", "SKILL.md"))
        self.ok(a, "sync")
        self.assertIn("B tail", a.read("codex", "alpha", "SKILL.md"))
        self.assertEqual(a.read("codex", "alpha", "run.py"), "print('v2 from A')\n")

    def test_non_overlapping_lines_merge(self):
        """AC-05：同文件非重叠段落合并，不产生冲突标记。"""
        a, b = self._two_copies()
        a.write_skill("codex", "alpha", {
            "SKILL.md": "# Alpha\n\nline1\nline2\nline3\nline4\nline5\nA tail\n",
        })
        self.ok(a, "sync")
        b.write_skill("codex", "alpha", {
            "SKILL.md": "# Alpha B title\n\nline1\nline2\nline3\nline4\nline5\n",
        })
        data = self.ok(b, "sync")
        self.assertIn("alpha", data["data"]["merged"])
        text = b.read("codex", "alpha", "SKILL.md")
        self.assertIn("A tail", text)
        self.assertIn("Alpha B title", text)
        self.assertNotIn("<<<<<<<", text)

    def test_same_region_conflict_then_resolve(self):
        """AC-06：同段冲突保留双方与基线；不部署冲突标记；批准后按指定结果。"""
        a, b = self._two_copies()
        a.write_skill("codex", "alpha", {"SKILL.md": "# Alpha\n\nshared line v2 from A\n"})
        self.ok(a, "sync")
        b.write_skill("codex", "alpha", {"SKILL.md": "# Alpha\n\nshared line v2 from B\n"})
        b_before = b.read("codex", "alpha", "SKILL.md")
        rc, data = b.run("sync")
        self.assertEqual(rc, 3, data)
        conflict = data["data"]["conflicts"][0]
        self.assertEqual(conflict["skill_id"], "alpha")
        cdir = b.base / "state" / "conflicts" / conflict["conflict_id"]
        for sub in ("local", "remote", "base"):
            self.assertTrue((cdir / sub).is_dir(), sub)
        self.assertTrue((cdir / "diff.txt").exists())
        # 冲突期间不改动本机文件、不写入冲突标记
        self.assertEqual(b.read("codex", "alpha", "SKILL.md"), b_before)
        self.assertNotIn("<<<<<<<", b_before)

        self.ok(b, "resolve", "alpha", "--take", "remote")
        data = self.ok(b, "continue")
        self.assertIn("alpha", data["data"]["applied"])
        self.assertEqual(b.read("codex", "alpha", "SKILL.md"), "# Alpha\n\nshared line v2 from A\n")
        self.assertEqual(b.state()["conflicts"], {})
        self.ok(a, "sync")
        self.assertEqual(a.read("codex", "alpha", "SKILL.md"), "# Alpha\n\nshared line v2 from A\n")

    def test_binary_conflict_never_overwrites(self):
        """AC-07 补充：二进制同改冲突不按时间覆盖。"""
        a, b = self.two_devices()
        a.write_skill("codex", "bin", {"SKILL.md": "# Bin\n"}, binary={"data.bin": b"\x00\x01"})
        self.allow_all(a)
        self.ok(a, "sync")
        self.ok(b, "sync")
        a.write_skill("codex", "bin", {}, binary={"data.bin": b"\x00\x02"})
        self.ok(a, "sync")
        b.write_skill("codex", "bin", {}, binary={"data.bin": b"\x00\x03"})
        rc, data = b.run("sync")
        self.assertEqual(rc, 3)
        self.assertEqual((b.roots["codex"] / "bin" / "data.bin").read_bytes(), b"\x00\x03")
        self.assertTrue(data["data"]["conflicts"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
