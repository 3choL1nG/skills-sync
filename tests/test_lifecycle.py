# -*- coding: utf-8 -*-
"""AC-08/09/10：本机卸载、人工删除、全局删除与离线设备冲突。"""
from __future__ import annotations

import json
import unittest

from harness import SyncTestCase, git


class LifecycleTest(SyncTestCase):
    def _shared_skill(self):
        a, b = self.two_devices()
        a.write_skill("codex", "alpha", self.skill_md("Alpha", "alpha v1"))
        a.write_skill("codex", "beta", self.skill_md("Beta", "beta v1"))
        self.allow_all(a)
        self.ok(a, "sync")
        self.ok(b, "sync")
        return a, b

    def test_uninstall_is_local_and_not_reinstalled(self):
        """AC-08：普通卸载只影响本机、记入不安装清单、不自动装回。"""
        a, b = self._shared_skill()
        self.ok(b, "uninstall", "alpha", "--yes")
        self.assertFalse((b.roots["codex"] / "alpha").exists())
        self.assertTrue(b.state()["prefs"]["alpha"]["uninstalled"])

        data = self.ok(a, "sync")
        self.assertTrue((a.roots["codex"] / "alpha").exists())
        self.assertNotIn("alpha", data["data"]["deleted_local"])

        data = self.ok(b, "sync")
        self.assertFalse((b.roots["codex"] / "alpha").exists(), "卸载后不应自动装回")
        self.assertIn("alpha", [x["skill_id"] for x in data["data"]["skipped"]])

        data = self.ok(b, "restore", "alpha")
        self.assertIn("alpha", data["data"]["restored"])
        self.assertTrue((b.roots["codex"] / "alpha" / "SKILL.md").exists())

    def test_manual_delete_marks_uninstalled_locally(self):
        """AC-08：人工删目录视为本机卸载，不传播到其他设备。"""
        a, b = self._shared_skill()
        import shutil
        shutil.rmtree(b.roots["codex"] / "alpha")
        data = self.ok(b, "sync")
        self.assertIn("alpha", data["data"]["marked_uninstalled"])
        self.ok(a, "sync")
        self.assertTrue((a.roots["codex"] / "alpha").exists())
        data = self.ok(b, "sync")
        self.assertFalse((b.roots["codex"] / "alpha").exists())

    def test_delete_shared_propagates_and_is_recoverable(self):
        """AC-09：delete-shared 全局删除并传播，其他设备删除；可用备份恢复。"""
        a, b = self._shared_skill()
        data = self.ok(a, "delete-shared", "beta", "--yes")
        self.assertIn("beta", data["data"]["deleted"])
        data = self.ok(b, "sync")
        self.assertIn("beta", data["data"]["deleted_local"])
        self.assertFalse((b.roots["codex"] / "beta").exists())
        rc, data = b.run("restore", "beta")
        self.assertNotEqual(rc, 0)
        self.assertIn("全局删除", data.get("error", ""))
        # 备份仍存在，可人工找回
        backups = list((b.base / "state" / "backups").rglob("*"))
        self.assertTrue(any(p.name == "SKILL.md" for p in backups if p.is_file()))

    def test_offline_device_delete_modify_conflict(self):
        """AC-10：离线设备改旧版后上线，报删除/修改冲突而不是静默覆盖。"""
        a, b = self._shared_skill()
        # B 离线期间改 alpha
        b.write_skill("codex", "alpha", {"SKILL.md": "# Alpha\n\noffline edit by B\n"})
        # A 全局删除 alpha
        self.ok(a, "delete-shared", "alpha", "--yes")
        # B 上线：本地有修改 + 共享库删除 → 冲突
        rc, data = b.run("sync")
        self.assertEqual(rc, 3, data)
        kinds = [c["kind"] for c in data["data"]["conflicts"]]
        self.assertIn("delete-modify", kinds)
        self.assertEqual(b.read("codex", "alpha", "SKILL.md"), "# Alpha\n\noffline edit by B\n")
        # 选择保留本机版本，重新纳入共享库
        self.ok(b, "resolve", "alpha", "--take", "local")
        data = self.ok(b, "continue")
        self.assertIn("alpha", data["data"]["applied"])
        # A 此前跟随全局删除移除了本地副本，且被记为"不安装"：不会自动装回
        data = self.ok(a, "sync")
        self.assertFalse((a.roots["codex"] / "alpha").exists())
        data = self.ok(a, "restore", "alpha")
        self.assertIn("alpha", data["data"]["restored"])
        self.assertEqual(a.read("codex", "alpha", "SKILL.md"), "# Alpha\n\noffline edit by B\n")


class SafetyTest(SyncTestCase):
    def test_fetch_failure_keeps_local_untouched(self):
        """AC-13：拉取失败明确报告，本机内容不被改动。"""
        a, b = self.two_devices()
        a.write_skill("codex", "alpha", self.skill_md("Alpha"))
        self.allow_all(a)
        self.ok(a, "sync")
        before = (b.roots["codex"]).glob("**/*")
        import configparser

        cfg = __import__("json").loads((b.config).read_text(encoding="utf-8"))
        cfg["remote_url"] = "http://127.0.0.1:9/remote.git"
        b.config.write_text(__import__("json").dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        rc, data = b.run("sync")
        self.assertEqual(rc, 2, data)
        self.assertFalse((b.roots["codex"] / "alpha").exists())
        self.assertTrue(data.get("error"))

    def test_push_rejected_then_retry(self):
        """AC-13：推送被拒 → 明确失败、不部署；恢复后端上重试成功。"""
        a, b = self.two_devices()
        a.write_skill("codex", "alpha", self.skill_md("Alpha"))
        self.allow_all(a)
        self.ok(a, "sync")
        self.ok(b, "sync")  # B 先安装并建立基线
        # 关闭 receive-pack 模拟推送被拒
        bare = self.world.remote_root / "remote.git"
        git("-C", str(bare), "config", "http.receivepack", "false")
        b.write_skill("codex", "alpha", {"SKILL.md": "# Alpha\n\nlocal change\n"})
        rc, data = b.run("sync")
        self.assertEqual(rc, 2, data)
        self.assertIn("推送", json.dumps(data, ensure_ascii=False))
        # 本机内容未被回滚，远端没有这次改动
        self.assertEqual(b.read("codex", "alpha", "SKILL.md"), "# Alpha\n\nlocal change\n")
        shown = git("-C", str(bare), "show", "main:skills/alpha/SKILL.md", check=False).stdout
        self.assertNotIn("local change", shown)
        # 恢复后重试：上次失败的本地提交被补推，历史不丢
        git("-C", str(bare), "config", "http.receivepack", "true")
        data = self.ok(b, "sync")
        shown = git("-C", str(bare), "show", "main:skills/alpha/SKILL.md").stdout
        self.assertIn("local change", shown)
        log = git("-C", str(bare), "log", "--oneline", "main").stdout
        self.assertGreaterEqual(len(log.strip().splitlines()), 3)


    def test_concurrent_push_does_not_force_overwrite(self):
        """AC-13：另一客户端抢先推送后，重算计划再推送，不强推丢历史。"""
        a, b = self.two_devices()
        a.write_skill("codex", "alpha", self.skill_md("Alpha", "alpha v1"))
        a.write_skill("codex", "beta", self.skill_md("Beta", "beta v1"))
        self.allow_all(a)
        self.ok(a, "sync")
        self.ok(b, "sync")
        bare = self.world.remote_root / "remote.git"
        # A 改 alpha，但推送阶段注入故障：本地留下一个未推送的提交
        a.write_skill("codex", "alpha", {"SKILL.md": "# Alpha\n\nalpha v2 by A\n"})
        rc, data = a.run("sync", extra_env={"SKILLS_SYNC_FAULT": "push"})
        self.assertEqual(rc, 2, data)
        self.assertIn("故障注入", json.dumps(data, ensure_ascii=False))
        # B 抢先推送另一处改动，远端前进
        b.write_skill("codex", "beta", {"SKILL.md": "# Beta\n\nbeta v2 by B\n"})
        self.assertIn("beta", self.ok(b, "sync")["data"]["pushed"])
        b_commit = git("-C", str(bare), "rev-parse", "main").stdout.strip()
        # A 再同步：拉取 B 的提交、重新应用自己的改动，然后正常推送
        data = self.ok(a, "sync")
        self.assertIn("alpha", data["data"]["pushed"])
        self.assertIn("alpha v2 by A", git("-C", str(bare), "show", "main:skills/alpha/SKILL.md").stdout)
        self.assertIn("beta v2 by B", git("-C", str(bare), "show", "main:skills/beta/SKILL.md").stdout)
        # B 的提交仍是远端祖先：历史没有被强推覆盖
        self.assertEqual(
            git("-C", str(bare), "merge-base", "--is-ancestor", b_commit, "main", check=False).returncode, 0)
        # A 也拿到了 B 的改动
        self.assertIn("beta v2 by B", a.read("codex", "beta", "SKILL.md"))


class CompatTest(SyncTestCase):
    def test_compat_hints(self):
        """AC-16：跨平台兼容提示覆盖 Windows 绝对路径/PS 脚本/macOS 工具。"""
        a, _ = self.two_devices()
        a.write_skill("codex", "crossplat", {
            "SKILL.md": "# Cross\n\n打开 C:\\\\Users\\\\me\\\\file.txt 或 /Users/me/file.txt\n",
            "scripts/run.ps1": "Write-Host hi\n",
            "scripts/mac.sh": "#!/bin/bash\nosascript -e 'beep'\n",
        })
        rc, data = a.run("plan")
        self.assertIn(rc, (0, 3))
        findings = []
        for preview in data["data"].get("compat", []):
            if preview["id"] == "crossplat":
                findings = preview["findings"]
        codes = {f["code"] for f in findings}
        self.assertTrue({"win_path", "powershell_script", "mac_only_tool"} <= codes, codes)
        data = self.ok(a, "compat")
        self.assertTrue(any(x["id"] == "crossplat" for x in data["data"]["skills"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
