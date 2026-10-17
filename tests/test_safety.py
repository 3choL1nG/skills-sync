# -*- coding: utf-8 -*-
"""AC-02/07/11/12/14/20：排除规则、首次同名冲突、路径边界、写入失败与过期批准。"""
from __future__ import annotations

import json
import os
import unittest

from harness import SyncTestCase, git

FAKE_TOKEN = "ghp_" + "A" * 36


class FilterTest(SyncTestCase):
    def test_system_runtime_and_secret_files_excluded(self):
        """AC-11：系统目录、缓存、运行产物、假凭据不进入共享库；模板资源保留。"""
        a, b = self.two_devices()
        skill = {
            "SKILL.md": "# Filter\n\nkeeps templates.\n",
            "scripts/run.py": "print('ok')\n",
            "assets/template.tpl": "{{ name }}\n",
        }
        target = a.write_skill("codex", "filtered", skill)
        (target / "__pycache__").mkdir()
        (target / "__pycache__" / "run.cpython-312.pyc").write_bytes(b"\x00pyc")
        (target / ".pytest_cache").mkdir()
        (target / ".pytest_cache" / "v").mkdir()
        (target / "output").mkdir()
        (target / "output" / "result.json").write_text("{}", encoding="utf-8")
        (target / ".env").write_text(f"GITHUB_TOKEN={FAKE_TOKEN}\n", encoding="utf-8")
        (target / "id_rsa").write_text("not a real key\n", encoding="utf-8")
        (target / ".DS_Store").write_bytes(b"\x00\x01")
        # 根级系统目录/隐藏目录/没有 SKILL.md 的目录都不算 skill
        system = a.roots["codex"] / ".system" / "builtin"
        system.mkdir(parents=True)
        (system / "SKILL.md").write_text("# builtin\n", encoding="utf-8")
        hidden = a.roots["codex"] / ".hidden" / "x"
        hidden.mkdir(parents=True)
        (hidden / "SKILL.md").write_text("# hidden\n", encoding="utf-8")
        plain = a.roots["codex"] / "not-a-skill"
        plain.mkdir()
        (plain / "readme.txt").write_text("hi\n", encoding="utf-8")

        data = self.plan(a)
        ids = {i["id"] for i in data["data"]["items"]}
        self.assertIn("filtered", ids)
        self.assertNotIn("builtin", ids)
        self.assertNotIn("hidden", ids)
        self.assertNotIn("not-a-skill", ids)
        item = next(i for i in data["data"]["items"] if i["id"] == "filtered")
        self.assertGreaterEqual(item["flags"]["excluded_dirs_count"], 2)
        self.assertGreaterEqual(len(item["flags"]["secret_files"]), 2)

        self.allow_all(a)
        self.ok(a, "sync")
        bare = self.world.remote_root / "remote.git"
        tree = git("-C", str(bare), "ls-tree", "-r", "--name-only", "main").stdout
        self.assertIn("skills/filtered/SKILL.md", tree)
        self.assertIn("skills/filtered/assets/template.tpl", tree)
        self.assertIn("skills/filtered/scripts/run.py", tree)
        for bad in ("__pycache__", ".pyc", ".pytest_cache", ".env", "id_rsa", ".DS_Store", "output/"):
            self.assertNotIn(bad, tree)
        grep = git("-C", str(bare), "grep", "-I", FAKE_TOKEN, "main", check=False).stdout
        self.assertNotIn(FAKE_TOKEN, grep)

    def test_third_party_snapshot_not_refreshed_from_upstream(self):
        """AC-02：第三方 skill 带 .git 与声明来源时，只同步本地快照，不去抓上游。"""
        a, b = self.two_devices()
        target = a.write_skill("codex", "vendor", {
            "SKILL.md": "# Vendor\n\nsource: https://github.com/example/vendor-skill\n",
            "scripts/tool.py": "print('patched by user')\n",
            "LICENSE": "MIT License\n",
        })
        (target / ".git").mkdir()
        (target / ".git" / "config").write_text("[core]\n", encoding="utf-8")
        data = self.plan(a)
        item = next(i for i in data["data"]["items"] if i["id"] == "vendor")
        self.assertEqual(item["source"]["type"], "declared")
        self.assertIn("github.com/example/vendor-skill", item["source"]["url"])
        self.allow_all(a)
        self.ok(a, "sync")
        self.ok(b, "sync")
        self.assertEqual(b.read("codex", "vendor", "scripts/tool.py"), "print('patched by user')\n")
        self.assertEqual(b.read("codex", "vendor", "LICENSE"), "MIT License\n")
        self.assertFalse((b.roots["codex"] / "vendor" / ".git").exists())


class ConflictBoundaryTest(SyncTestCase):
    def test_first_import_same_name_different_content(self):
        """AC-07：首次汇总遇到同名异内容，不按时间戳覆盖，保留双方并暂停。"""
        a, b = self.two_devices()
        a.write_skill("codex", "alpha", {"SKILL.md": "# Alpha\n\nfrom A\n"})
        b.write_skill("codex", "alpha", {"SKILL.md": "# Alpha\n\nfrom B\n"})
        self.allow_all(a)
        self.ok(a, "sync")
        rc, data = b.run("sync")
        self.assertEqual(rc, 3, data)
        conflicts = data["data"]["conflicts"]
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["skill_id"], "alpha")
        self.assertEqual(conflicts[0]["kind"], "first-import")
        self.assertEqual(b.read("codex", "alpha", "SKILL.md"), "# Alpha\n\nfrom B\n")
        cid = conflicts[0]["conflict_id"]
        cdir = b.base / "state" / "conflicts" / cid
        self.assertTrue((cdir / "local" / "SKILL.md").is_file())
        self.assertTrue((cdir / "remote" / "SKILL.md").is_file())
        self.assertTrue((cdir / "diff.txt").is_file())
        self.ok(b, "resolve", cid, "--take", "remote")
        self.ok(b, "continue")
        self.assertEqual(b.read("codex", "alpha", "SKILL.md"), "# Alpha\n\nfrom A\n")

    def test_case_collision_refused_without_overwrite(self):
        """AC-12：仅大小写/Unicode 差异的目录不会被静默覆盖。"""
        a, b = self.two_devices()
        a.write_skill("codex", "Foo", {"SKILL.md": "# Foo upper\n"})
        a.write_skill("agents", "foo", {"SKILL.md": "# foo lower\n"})
        rc, data = a.run("plan")
        self.assertEqual(rc, 3, data)
        self.assertTrue(data["data"]["collisions"])
        rc, data = a.run("sync")
        self.assertNotEqual(rc, 0, data)
        bare = self.world.remote_root / "remote.git"
        tree = git("-C", str(bare), "ls-tree", "-r", "--name-only", "main").stdout
        self.assertNotIn("skills/foo/SKILL.md", tree)
        self.assertNotIn("skills/Foo/SKILL.md", tree)
        hub_skills = a.base / "hub" / "skills"
        self.assertFalse((hub_skills / "Foo").exists())
        self.assertFalse((hub_skills / "foo").exists())
        self.assertFalse((a.base / "state" / "skills" / "foo" / "baseline.json").exists())

    def test_space_and_chinese_paths_round_trip(self):
        """AC-12：空格、中文目录与文件名往返后内容一致。"""
        a, b = self.two_devices()
        a.write_skill("codex", "中文 技能", {
            "SKILL.md": "# 中文 技能\n\n路径测试。\n",
            "参考 文档/说明 一.md": "内容 一致\n",
            "scripts/运行 一.ps1": "Write-Host '你好'\n",
        })
        self.allow_all(a)
        self.ok(a, "sync")
        self.ok(b, "sync")
        self.assertEqual(b.read("codex", "中文 技能", "参考 文档/说明 一.md"), "内容 一致\n")
        self.assertEqual(b.read("codex", "中文 技能", "scripts/运行 一.ps1"), "Write-Host '你好'\n")
        self.assertEqual(b.read("codex", "中文 技能", "SKILL.md"), "# 中文 技能\n\n路径测试。\n")


class FailureTest(SyncTestCase):
    def test_install_write_failure_keeps_backup_and_reports_failure(self):
        """AC-14：部署写入失败时保留可恢复备份、不标记成功、本机旧内容不丢。"""
        a, b = self.two_devices()
        a.write_skill("codex", "alpha", {"SKILL.md": "# Alpha\n\nv1\n"})
        self.allow_all(a)
        self.ok(a, "sync")
        self.ok(b, "sync")
        self.assertEqual(b.read("codex", "alpha", "SKILL.md"), "# Alpha\n\nv1\n")

        a.write_skill("codex", "alpha", {"SKILL.md": "# Alpha\n\nv2 from A\n"})
        self.ok(a, "sync")

        rc, data = b.run("sync", extra_env={"SKILLS_SYNC_FAULT": "deploy-write"})
        self.assertEqual(rc, 2, data)
        self.assertIn(data["status"], ("partial", "failed"))
        self.assertIn("alpha", [f["skill_id"] for f in data["data"]["failures"]])
        self.assertEqual(b.read("codex", "alpha", "SKILL.md"), "# Alpha\n\nv1\n")
        op_id = data["data"]["op_id"]
        backups = b.base / "state" / "backups" / op_id
        self.assertTrue(any(p.name == "SKILL.md" for p in backups.rglob("SKILL.md")))
        op = json.loads((b.base / "state" / "ops" / f"{op_id}.json").read_text(encoding="utf-8"))
        self.assertIn(op["status"], ("partial", "failed"))

        self.ok(b, "sync")
        self.assertEqual(b.read("codex", "alpha", "SKILL.md"), "# Alpha\n\nv2 from A\n")

    def test_stale_approval_does_not_override_new_content(self):
        """AC-20：批准后内容又改了，旧批准不得把新内容推上去。"""
        a, b = self.two_devices()
        a.write_skill("codex", "gamma", {"SKILL.md": "# Gamma\n\nfirst\n"})
        self.plan(a)
        self.ok(a, "include", "gamma")
        a.write_skill("codex", "gamma", {"SKILL.md": "# Gamma\n\nchanged after approval\n"})
        rc, data = a.run("sync")
        self.assertEqual(rc, 3, data)
        bare = self.world.remote_root / "remote.git"
        tree = git("-C", str(bare), "ls-tree", "-r", "--name-only", "main").stdout
        self.assertNotIn("skills/gamma", tree)
        rc, plan = a.run("plan")
        self.assertEqual(rc, 3, plan)
        self.assertIn("gamma", [i["id"] for i in plan["data"]["items"] if i["action"] == "pending_add"])

    def test_path_escape_rejected(self):
        """AC-20：越界 id 与路径被拒绝，不产生读写。"""
        a, b = self.two_devices()
        for bad in ("../evil", "..", "a/b", "C:\\Windows"):
            rc, data = a.run("include", bad)
            self.assertEqual(rc, 1, data)
            self.assertIn("非法", json.dumps(data, ensure_ascii=False))
        rc, data = a.run("delete-shared", "../evil", "--yes")
        self.assertEqual(rc, 1, data)

    def test_symlink_entries_not_followed(self):
        """AC-20：skill 内的符号链接不会被跟随、复制或写入共享库。"""
        a, b = self.two_devices()
        target = a.write_skill("codex", "linked", {"SKILL.md": "# Linked\n\nbody\n"})
        outside = self.world.base / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("OUTSIDE-SECRET\n", encoding="utf-8")
        link = target / "escape"
        try:
            os.symlink(outside, link, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:  # Windows 未开启开发者模式
            self.skipTest(f"无法创建符号链接：{exc}")
        data = self.plan(a)
        item = next(i for i in data["data"]["items"] if i["id"] == "linked")
        self.assertTrue(item["flags"]["links"])
        self.allow_all(a)
        self.ok(a, "sync")
        bare = self.world.remote_root / "remote.git"
        tree = git("-C", str(bare), "ls-tree", "-r", "--name-only", "main").stdout
        self.assertNotIn("escape", tree)
        grep = git("-C", str(bare), "grep", "-I", "OUTSIDE-SECRET", "main", check=False).stdout
        self.assertNotIn("OUTSIDE-SECRET", grep)


if __name__ == "__main__":
    unittest.main()
