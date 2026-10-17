"""返工-02 的正式回归：BUG-01/04/06/07/08/09 各一条可重复用例。

这些用例把第二轮验收里"实测缺陷"的场景固化成正式测试（不是只跑一次的手工探针）：
同样的场景在 `evidence/codex-probes/repair_scope_probes.py` 里也有一份逐字保留的
版本，两者互为交叉验证；断言都不依赖文件是否存在，而是看内容是否被保住、
共享库/基线是否真的改变。
"""
from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import SyncTestCase, git

from skills_sync import util
from skills_sync.config import Config
from skills_sync.engine import Engine
from skills_sync.errors import ConflictPending, StateError

DOC = Path(__file__).resolve().parents[1] / "skill" / "skills-sync" / "SKILL.md"


class Rework02Tests(SyncTestCase):
    def shared(self):
        """两台设备都装着同一份 alpha，基线一致。"""
        a, b = self.two_devices()
        a.write_skill("codex", "alpha", {"SKILL.md": "# Alpha\n\nbase\n"})
        self.allow_all(a)
        self.ok(a, "sync")
        self.ok(b, "sync")
        return a, b

    def bare(self) -> Path:
        return self.world.remote_root / "remote.git"

    # ---------------------------------------------------------------- BUG-01
    def test_entry_skill_table_covers_every_real_action(self):
        """入口 skill 的动作表必须覆盖真实会出现的 action（含远端新增的 deploy）。"""
        a, b = self.two_devices()
        a.write_skill("codex", "alpha", {"SKILL.md": "# Alpha\n\nbase\n"})
        self.allow_all(a)
        self.ok(a, "sync")
        a.write_skill("codex", "alpha", {"SKILL.md": "# Alpha\n\nremote v2\n"})
        self.ok(a, "sync")

        plan = self.plan(b)["data"]
        self.assertEqual([item["action"] for item in plan["items"]], ["deploy"])

        doc = DOC.read_text(encoding="utf-8")
        for action in ("deploy", "push", "push_new", "merge", "adopt", "delete_local",
                       "mark_uninstalled", "unchanged", "pending_add", "conflict", "error"):
            self.assertIn(f"| `{action}` |", doc, f"入口 skill 的动作表缺少 {action}")
        self.assertIn("`skip_local_only`", doc)
        self.assertIn("`skip_uninstalled`", doc)
        # 曾把"只有 push/pull/merge 才 sync"和"total==0 即最新"写成规则；
        # 现在必须明确否掉 total==0 这种判断，且不再出现 pull 这个不存在的动作。
        self.assertIn("不能用 `summary.total == 0`", doc)
        self.assertNotIn("`pull`", doc)
        self.assertIn("`stale`", doc)

    # ---------------------------------------------------------------- BUG-04
    def test_edit_while_staging_is_not_overwritten(self):
        """准备部署副本期间的编辑必须保住，且不能报 ok。"""
        a, b = self.shared()
        a.write_skill("codex", "alpha", {"SKILL.md": "# Alpha\n\nremote v2\n"})
        self.ok(a, "sync")

        engine = Engine(Config.load(b.config))
        original = util.copy_tree
        injected = []

        def copy_then_edit(src, dst):
            result = original(src, dst)
            if Path(src) == engine.hub / "skills" / "alpha" and "deploy-alpha" in Path(dst).name:
                b.write_skill("codex", "alpha", {"SKILL.md": "# Alpha\n\nEDIT DURING STAGING\n"})
                injected.append(True)
            return result

        with patch.object(util, "copy_tree", side_effect=copy_then_edit):
            result = engine.sync()

        self.assertTrue(injected, "注入点没有命中：部署副本没有经过 copy_tree")
        self.assertIn("EDIT DURING STAGING", b.read("codex", "alpha", "SKILL.md"))
        self.assertNotEqual(result["status"], "ok")
        self.assertTrue(result["stale"])

    def test_edit_after_continue_push_is_kept_and_reported_undeployed(self):
        """推送成功但落地前又被编辑：保住本机、报"未部署"、基线不前移。"""
        a, b = self.shared()
        a.write_skill("codex", "alpha", {"SKILL.md": "# Alpha\n\nA choice\n"})
        b.write_skill("codex", "alpha", {"SKILL.md": "# Alpha\n\nB choice\n"})
        self.ok(a, "sync")
        rc, _ = b.run("sync")
        self.assertEqual(rc, 3)
        self.ok(b, "resolve", "alpha", "--take", "local")

        baseline_before = b.state()["baselines"]["alpha"]["hash"]
        engine = Engine(Config.load(b.config))
        original_push = engine.git.push
        pushed = []

        def push_then_edit(hub):
            original_push(hub)
            b.write_skill("codex", "alpha", {"SKILL.md": "# Alpha\n\nEDIT DURING PUSH\n"})
            pushed.append(True)

        engine.git.push = push_then_edit
        with self.assertRaises(ConflictPending) as caught:
            engine.continue_()

        self.assertTrue(pushed)
        self.assertIn("EDIT DURING PUSH", b.read("codex", "alpha", "SKILL.md"))
        self.assertIn("本机未部署", caught.exception.message)
        self.assertEqual(b.state()["baselines"]["alpha"]["hash"], baseline_before)

    # ---------------------------------------------------------------- BUG-06
    def test_refreshed_global_delete_approval_still_deletes(self):
        """批准失效后重建的冲突里，"接受远端"仍然等于"批准这次全局删除"。"""
        a, b = self.shared()
        a.write_skill("codex", "alpha", {"SKILL.md": "# Alpha\n\nlocal v2\n"})
        rc, _ = a.run("delete-shared", "alpha", "--yes")
        self.assertEqual(rc, 3)
        self.ok(a, "resolve", "alpha", "--take", "remote")

        a.write_skill("codex", "alpha", {"SKILL.md": "# Alpha\n\nlocal v3\n"})
        rc, _ = a.run("continue")
        self.assertEqual(rc, 3, "本机又改了，旧批准应当失效")

        resolved = self.ok(a, "resolve", "alpha", "--take", "remote")["data"]
        self.assertEqual(resolved["resolved"][0]["mode"], "approve_delete")
        self.ok(a, "continue")
        self.ok(b, "sync")

        man = json.loads(git("-C", str(self.bare()), "show", "main:manifest.json").stdout)
        self.assertTrue(man["skills"]["alpha"]["deleted"], "共享库应留下删除墓碑")
        self.assertFalse((a.roots["codex"] / "alpha").exists())
        self.assertFalse((b.roots["codex"] / "alpha").exists())

    # ---------------------------------------------------------------- BUG-07
    def test_recover_copy_failure_keeps_current_target(self):
        """恢复失败不得把当前目录变成"不存在"，且必须如实报失败。"""
        a, b = self.shared()
        op_id = self.ok(b, "uninstall", "alpha", "--yes")["data"]["op_id"]
        self.ok(b, "restore", "alpha")  # 当前内容 == 备份内容，满足 recover 的前置条件
        target = b.roots["codex"] / "alpha" / "SKILL.md"
        before = target.read_text(encoding="utf-8")

        engine = Engine(Config.load(b.config))
        with patch.object(util, "copy_tree", side_effect=PermissionError("denied")):
            with self.assertRaises(StateError) as caught:
                engine.recover(restore=f"{op_id}/alpha")

        self.assertIn("未能完整写回", caught.exception.message)
        self.assertTrue(target.is_file(), "恢复失败后目标目录必须仍然可用")
        self.assertEqual(target.read_text(encoding="utf-8"), before)

    # ---------------------------------------------------------------- BUG-08
    def test_copy_selected_rejects_intermediate_junction(self):
        """中间目录被换成 junction 时，复制必须拒绝（不能把根外内容复制出来）。"""
        base = self.world.base
        source = base / "source"
        (source / "refs").mkdir(parents=True)
        (source / "refs" / "data.txt").write_text("INSIDE ORIGINAL\n", encoding="utf-8")
        outside = base / "outside-root"
        outside.mkdir()
        (outside / "data.txt").write_text("SYNTHETIC OUTSIDE DATA\n", encoding="utf-8")

        files = util.snapshot_dir(source)
        (source / "refs").rename(source / "saved-refs")
        made = subprocess.run(["cmd", "/c", "mklink", "/J", str(source / "refs"), str(outside)],
                              capture_output=True, text=True)
        if made.returncode != 0:
            self.skipTest(f"当前环境无法创建 junction：{made.stderr or made.stdout}")

        dest = base / "copy"
        with self.assertRaises(ValueError):
            util.copy_selected(source, files, dest)
        self.assertFalse((dest / "refs" / "data.txt").exists())

    # ---------------------------------------------------------------- BUG-09
    def test_include_root_binds_source_for_later_sync(self):
        """`include <id> --root <key>` 之后，sync 必须真的用那个 root 的内容。"""
        a, b = self.two_devices()
        a.write_skill("codex", "vendor", {"SKILL.md": "# Vendor\n\nCODEX SOURCE\n"})
        a.write_skill("agents", "vendor", {"SKILL.md": "# Vendor\n\nAGENTS SOURCE\n"})

        approved = self.ok(a, "include", "vendor", "--root", "agents")["data"]
        self.assertEqual(approved["results"][0]["root"], "agents")
        self.assertEqual(a.state()["approved_additions"]["vendor"]["root"], "agents")

        self.ok(a, "sync")
        self.ok(b, "sync")

        self.assertEqual(b.read("codex", "vendor", "SKILL.md"), "# Vendor\n\nAGENTS SOURCE\n")
        # 没被选中的 root 原样保留，且计划里要说清楚
        self.assertEqual(a.read("codex", "vendor", "SKILL.md"), "# Vendor\n\nCODEX SOURCE\n")
        item = next(i for i in self.plan(a)["data"]["items"] if i["id"] == "vendor")
        self.assertEqual(item["action"], "unchanged")
        self.assertTrue(any("其他 root" in w for w in item["warnings"]), item["warnings"])

    def test_include_root_documented_command_propagates(self):
        """文档里写的 `include <id> --root <key>` 单来源场景必须真的传播到另一台设备。"""
        a, b = self.two_devices()
        a.write_skill("agents", "vendor", {"SKILL.md": "# Vendor\n\nDOC COMMAND\n"})

        self.ok(a, "include", "vendor", "--root", "agents")
        self.ok(a, "sync")
        self.ok(b, "sync")

        self.assertEqual(b.read("codex", "vendor", "SKILL.md"), "# Vendor\n\nDOC COMMAND\n")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
