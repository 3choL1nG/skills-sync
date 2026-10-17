"""同步引擎：计划、同步事务、审批、卸载/恢复、全局删除、冲突解决与恢复。

设计要点（详见 ``docs/环境与设计.md``）：

* 基线保存在状态目录（共享库之外）的 ``skills/<id>/base``，三方合并只比较
  base/local/remote 内容，从不使用文件时间戳。
* 任何覆盖本机目录的动作之前先备份；共享库推送成功后才部署；部署失败回滚且
  不写基线，避免把失败伪装成成功。
* 冲突候选保存在状态目录，解决结果绑定内容哈希；执行前再次校验，旧批准不会
  覆盖新改动。
"""

from __future__ import annotations

import os
import re
import shutil
import uuid
from pathlib import Path

from . import gitops, manifest, merge, scan, util
from .errors import (
    ConflictPending,
    DeployError,
    GitError,
    StateError,
    SyncError,
    UsageError,
    STATUS_FAILED,
    STATUS_NEEDS_ACTION,
    STATUS_OK,
    STATUS_PARTIAL,
)
from .state import State

APPROVAL_KEY = "approved_additions"
FAULT_ENV = "SKILLS_SYNC_FAULT"
MAX_COMPAT_PREVIEW = 40
MAX_DIFF_FILES = 60

_WIN_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WIN_RESERVED = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | {
    f"lpt{i}" for i in range(1, 10)
}


def _validate_skill_id(sid: str) -> None:
    """拒绝越界/非法 id：只允许单段名字，不允许路径分隔符、..、绝对路径。"""
    text = str(sid or "").strip()
    if (
        not text
        or text in (".", "..")
        or "/" in text
        or "\\" in text
        or ":" in text
        or text.startswith("~")
        or "\x00" in text
        or util.normalize_identity(text) != text
    ):
        raise UsageError(
            f"skill id 非法：{sid!r}",
            hint="id 应是 skill 目录名本身（如 `grill-me`），不接受路径、`..` 或绝对路径。",
        )


def safe_dir_name(name: str, fallback: str = "skill") -> str:
    """把共享名称转成可安全落盘的目录名（Windows 非法字符/保留名兜底）。"""
    text = _WIN_BAD_CHARS.sub("-", str(name or "")).strip().rstrip(". ")
    if not text or text.lower() in _WIN_RESERVED:
        text = fallback or "skill"
    return text[:120]


class Engine:
    def __init__(self, cfg, state: State | None = None):
        self.cfg = cfg
        self.state = state or State(cfg)
        self.git = gitops.Git(cfg)
        self._filters = scan.Filters(cfg)
        self._tmp_root = self.state.dir / "tmp"

    # ------------------------------------------------------------------ 公共工具

    @property
    def hub(self) -> Path:
        return Path(self.cfg.hub_dir)

    def _hub_ready(self) -> bool:
        return self.git.is_repo(self.hub)

    def _load_manifest(self) -> dict:
        try:
            return manifest.load(self.hub)
        except ValueError as exc:
            raise StateError(
                str(exc),
                hint="共享库 manifest.json 可能被改坏；可用 `skills-sync recover` 查看，"
                "必要时从 git 历史恢复该文件。",
            ) from exc

    def _approvals(self) -> dict:
        return self.state.data.setdefault(APPROVAL_KEY, {})

    def _excluder(self):
        filters = self._filters

        def exclude(rel: str) -> bool:
            return bool(filters.excluded(rel) or filters.secret_name(rel))

        return exclude

    def _tmp_dir(self, op: dict | None, tag: str) -> Path:
        """返回一个尚未创建的临时目录路径（由调用方 copy_tree/copy_selected 创建）。"""
        self._tmp_root.mkdir(parents=True, exist_ok=True)
        seq = 0
        base = self._tmp_root / (f"{op['op_id']}-{tag}" if op else f"manual-{tag}")
        path = base
        while path.exists():
            seq += 1
            path = base.with_name(f"{base.name}-{seq}")
        return path

    def _fault(self, point: str) -> None:
        """测试注入口：SKILLS_SYNC_FAULT=fetch,push,deploy-write,hub-write。"""
        spec = os.environ.get(FAULT_ENV, "")
        if not spec:
            return
        points = {part.strip() for part in spec.split(",") if part.strip()}
        if point not in points:
            return
        if point.startswith("deploy"):
            raise DeployError(f"[测试故障注入] {point} 失败（SKILLS_SYNC_FAULT）")
        raise GitError(f"[测试故障注入] {point} 失败（SKILLS_SYNC_FAULT）")

    def _filters_snapshot(self, root: Path) -> tuple[dict, str]:
        files = util.snapshot_dir(root, exclude=self._excluder())
        return files, util.tree_hash(files)

    def _snapshot_user_dir(self, root: Path) -> tuple[dict, str]:
        """对用户提供的目录做与扫描一致的过滤，并检查常见凭据内容。"""
        files: dict[str, dict] = {}
        for rel, full in util.list_tree(root):
            if self._excluder()(rel):
                continue
            text = util.read_text(full)
            if text is not None:
                hit = None
                for raw, rx in self._filters.secret_patterns:
                    if rx.search(text):
                        hit = raw
                        break
                if hit:
                    continue
            files[rel] = {"hash": util.sha256_file(full), "size": full.stat().st_size}
        return files, util.tree_hash(files)

    # ------------------------------------------------------------------ 仓库准备

    def _prepare_hub(self, *, fetch: bool, op: dict | None = None) -> bool:
        """确保共享库存在且工作区干净；返回是否本次新建。"""
        hub = self.hub
        if util.is_within(self.cfg.state_dir, hub) or util.is_within(hub, self.cfg.state_dir):
            raise StateError(
                "state_dir 与共享库目录不能互相包含：本机状态必须放在共享库之外。",
                hint="修改配置中的 state_dir（例如 ~/.skills-sync/state）后重试。",
            )
        created = False
        if not self.git.is_repo(hub):
            if self.cfg.remote_url:
                self._fault("fetch")
                self.git.clone(self.cfg.remote_url, hub, self.cfg.branch)
            else:
                self.git.init_repo(hub, self.cfg.branch)
            manifest.write_readme(hub)
            created = True
        elif self.cfg.remote_url:
            current = self.git.remote_url(hub)
            if not current or current != self.cfg.remote_url:
                # 配置里的远端地址优先：地址改了要跟着改 origin，否则会连到旧地址
                self.git.set_remote(hub, self.cfg.remote_url)

        if self.cfg.remote_url and fetch and not created:
            dirty = self.git.dirty(hub)
            if dirty:
                raise StateError(
                    "共享库工作区存在未提交改动，已停止同步以免覆盖。",
                    hint="运行 `skills-sync recover` 查看；确认无用后可 "
                    "`git -C <hub> reset --hard origin/<branch>`。",
                    details={"dirty": dirty[:30]},
                )
            self._fault("fetch")
            self.git.fetch(hub)
            self._reset_worktree(op)
        return created

    def _reset_worktree(self, op: dict | None) -> None:
        if not self.cfg.remote_url:
            return
        remote = self.git.remote_head(self.hub, self.cfg.branch)
        if not remote:
            return
        head = self.git.head(self.hub)
        if head == remote and self.git.branch_of(self.hub) == self.cfg.branch:
            return
        if head and self.git.is_ancestor(self.hub, remote, head):
            # 本地领先远端：存在上次推送失败的本地提交，保留历史，稍后重推
            return
        tag = f"skills-sync-backup-{(op or {}).get('op_id') or 'manual'}"
        self.git.reset_to_remote(self.hub, backup_tag=tag)

    # ------------------------------------------------------------------ 初始化

    def init(self) -> dict:
        """建立/克隆共享库，写初始 manifest，并在远端为空时推送首个提交。"""
        if not self.cfg.roots:
            raise UsageError("配置中没有 roots，无法初始化。")
        warnings = []
        for root in self.cfg.roots:
            path = Path(root["path"])
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise StateError(f"无法创建 skill 根目录 {path}：{exc}") from exc
        self.cfg.data["device_name"] = self.cfg.device_name
        self.cfg.save()
        self.state.save()

        created = self._prepare_hub(fetch=False)
        if self.git.is_repo(self.hub):
            manifest.write_readme(self.hub)
        commit = None
        pushed = False
        if self.git.is_repo(self.hub):
            commit = self.git.commit_all(self.hub, "skills-sync: initialize shared hub")
            if commit and self.cfg.remote_url:
                try:
                    self.git.push(self.hub)
                    pushed = True
                except GitError as exc:
                    raise GitError(
                        f"初始化推送失败：{exc.message}",
                        hint="本地共享库已建好；网络/权限恢复后重新运行 sync 或 init 即可推送。",
                        details={"commit": commit},
                    ) from exc
            if commit and not self.cfg.remote_url:
                warnings.append("未配置远端：当前是仅本机共享库，不会跨设备同步。")
        else:  # pragma: no cover - _prepare_hub 一定会建仓库
            warnings.append("共享库未就绪。")

        return {
            "status": STATUS_OK,
            "config": str(self.cfg.path),
            "device": self.cfg.device_name,
            "hub": str(self.hub),
            "state": str(self.state.dir),
            "remote_url": util.redact_url(self.cfg.remote_url),
            "branch": self.cfg.branch,
            "roots": [{"key": r["key"], "path": str(r["path"])} for r in self.cfg.roots],
            "hub_created": created,
            "initial_commit": commit,
            "pushed": pushed,
            "warnings": warnings,
            "next": "运行 `skills-sync plan` 查看本机 skill 与待批准的新增内容。",
        }

    # ------------------------------------------------------------------ 计划

    def plan(self, *, fetch: bool = False) -> dict:
        """只读计划：不写任何 skill 目录，也不改动/删除冲突证据。

        fetch=True 时先获取远端再计算（只更新本机共享库镜像，不在 skill 根写入）。
        没有检查远端时，计划会明确标注 stale，且不会被当成"已是最新"。
        """
        remote_checked = False
        remote_error = ""
        if fetch and self.cfg.remote_url:
            try:
                self._prepare_hub(fetch=True)
                remote_checked = True
            except (GitError, StateError) as exc:
                remote_error = exc.message
        elif not self.cfg.remote_url:
            remote_checked = True  # 没有远端时，本机共享库就是全部真相
        local_scan = scan.scan_roots(self.cfg)
        hub_scan = scan.scan_hub(self.hub) if self._hub_ready() else {}
        man = self._load_manifest() if self._hub_ready() else manifest.empty()
        items, ctx = self._build_plan(local_scan, hub_scan, man)
        for item in items:
            item.pop("_merge", None)
        warnings = self._plan_warnings(items, local_scan)
        if not remote_checked:
            warnings.append(
                "未确认远端最新状态"
                + (f"（{remote_error}）" if remote_error else "（本次未获取远端）")
                + "；当前计划只反映本机与上次已知的共享库快照，不能视为最新。"
            )
        needs_action = [i["id"] for i in items if i["needs_action"]]
        if not remote_checked:
            needs_action.append("remote-check")
        data = {
            "schema": 1,
            "kind": "plan",
            "status": STATUS_NEEDS_ACTION if (any(i["needs_action"] for i in items) or not remote_checked)
            else STATUS_OK,
            "device": self.cfg.device_name,
            "created_at": util.now_iso(),
            "config": str(self.cfg.path),
            "hub": str(self.hub),
            "hub_ready": self._hub_ready(),
            "remote_url": util.redact_url(self.cfg.remote_url),
            "branch": self.cfg.branch,
            "remote_checked": remote_checked,
            "remote_error": remote_error,
            "stale": not remote_checked,
            "hub_head": self.git.head(self.hub) if self._hub_ready() else None,
            "remote_head": self.git.remote_head(self.hub, self.cfg.branch)
            if self._hub_ready() and self.cfg.remote_url
            else None,
            "items": items,
            "summary": _summarize(items),
            "needs_action": needs_action,
            "compat": self._compat_preview(local_scan),
            "warnings": warnings,
            "collisions": local_scan["collisions"],
            "skipped": local_scan["skipped"],
            "filter_report": local_scan["filter_report"],
        }
        return data

    def _build_plan(self, local_scan: dict, hub_scan: dict, man: dict):
        filters = self._filters
        by_id: dict[str, list[dict]] = {}
        for entry in local_scan["skills"].values():
            by_id.setdefault(entry["id"], []).append(entry)
        for entries in by_id.values():
            entries.sort(key=lambda e: e["root"])

        collision_ids = {c["id"] for c in local_scan["collisions"]}
        same_name = {c["id"]: c for c in local_scan["same_name_conflicts"]}
        base_ids = set(self.state.data["baselines"])
        tomb_ids = {k for k, v in man.get("skills", {}).items() if v.get("deleted")}
        all_ids = sorted(set(by_id) | set(hub_scan) | base_ids | tomb_ids)

        items: list[dict] = []
        ctx: dict[str, dict] = {}
        for sid in all_ids:
            entries = by_id.get(sid, [])
            entry = self._pick_local(sid, entries)
            local_dir = Path(entry["path"]) if entry else None
            local_files = entry["files"] if entry else {}
            local_hash = entry["hash"] if entry else ""

            remote = hub_scan.get(sid)
            remote_dir = Path(remote["path"]) if remote else None
            remote_files = remote["files"] if remote else {}
            remote_hash = remote["hash"] if remote else ""

            base_meta = self.state.baseline(sid)
            base_hash = base_meta["hash"] if base_meta else ""
            base_dir = self.state.base_content_dir(sid)
            if base_meta and not base_dir.is_dir():
                raise StateError(
                    f"skill {sid!r} 的基线内容缺失（{base_dir}），无法安全合并。",
                    hint="运行 `skills-sync recover` 查看；可先 `skills-sync abort` 清理冲突后重新初始化基线。",
                )
            if not base_meta:
                base_dir = None

            meta = man.get("skills", {}).get(sid, {})
            name = (
                (entry["dir_name"] if entry else "")
                or meta.get("name")
                or sid
            )
            tomb = bool(meta.get("deleted"))
            item = {
                "id": sid,
                "name": name,
                "action": "unchanged",
                "kind": "",
                "root": "",
                "path": "",
                "local_hash": local_hash,
                "remote_hash": remote_hash,
                "base_hash": base_hash,
                "detail": "",
                "options": [],
                "needs_action": False,
                "warnings": [],
                "flags": {},
                "compat": len(entry["compat"]) if entry else 0,
                "source": (entry["source"] if entry else {}) or {},
                "licenses": list(entry["licenses"]) if entry else [],
            }
            ctx[sid] = {
                "entries": entries,
                "local": entry,
                "local_dir": local_dir,
                "local_files": local_files,
                "remote_dir": remote_dir,
                "remote_files": remote_files,
                "base_dir": base_dir,
                "base_files": self.state.baseline_files(sid) if base_meta else {},
                "tomb": tomb,
                "existing": None,
                "merge": None,
            }
            if entry:
                item["flags"] = {
                    "secret_files": [x["path"] for x in entry["secret_files"]][:50],
                    "secret_files_count": len(entry["secret_files"]),
                    "excluded_dirs": [x["path"] for x in entry["excluded_dirs"]][:50],
                    "excluded_dirs_count": len(entry["excluded_dirs"]),
                    "links": [x["path"] for x in entry["links"]][:50],
                }

            pref = self.state.pref(sid)
            uninstalled = bool(pref.get("uninstalled"))
            local_only = bool(pref.get("local_only"))
            approval = self._approvals().get(sid) or {}

            if sid in collision_ids:
                names = next(c["names"] for c in local_scan["collisions"] if c["id"] == sid)
                item.update(
                    action="error",
                    kind="case-collision",
                    needs_action=True,
                    detail=f"两个根下存在仅大小写/Unicode 差异的目录 {names}，"
                    "在某些文件系统上会互相覆盖；请手动改名后重试。",
                )
                items.append(item)
                continue

            if sid in same_name and entry is None:
                same = same_name[sid]
                # 用两个候选的内容哈希代表该冲突，便于内容变化时作废旧冲突
                item["local_hash"] = same["entries"][0]["hash"]
                item["remote_hash"] = same["entries"][1]["hash"]
                ctx[sid]["same_name"] = same

            # 计划所依据的完整输入指纹：本机/共享库/基线/墓碑/各根候选。
            # 冲突批准、部署与删除都以它为准做"计划后是否变化"的复核。
            candidates = {}
            if sid in same_name and entry is None:
                candidates = {e["root"]: e["hash"] for e in same_name[sid]["entries"]}
            item["input"] = {
                "local": {"exists": local_dir is not None, "hash": local_hash},
                "remote": {"exists": remote_dir is not None, "hash": remote_hash, "tomb": tomb},
                "base": {"exists": bool(base_meta), "hash": base_hash},
                "candidates": candidates,
            }
            ctx[sid]["input"] = item["input"]

            # --- 已有冲突优先：输入完全一致才复用旧证据 ---
            existing = self.state.get_conflict(sid)
            if existing:
                if self._conflict_input_matches(existing, item["input"]):
                    item.update(
                        action="conflict",
                        kind=existing["kind"],
                        detail=existing["detail"],
                        options=existing["options"],
                        needs_action=True,
                    )
                    ctx[sid]["existing"] = existing
                    items.append(item)
                    continue
                # 输入已变化：保留旧快照（只读，不删除），由后续流程按当前内容重建冲突
                item.update(
                    action="conflict",
                    kind=existing["kind"],
                    detail=existing["detail"] + "（冲突创建后输入已变化，旧批准失效；"
                    f"旧快照保留在 {self.state.conflict_dir(existing['conflict_id'])}）",
                    options=existing["options"],
                    needs_action=True,
                    stale=True,
                    supersedes=existing["conflict_id"],
                )
                items.append(item)
                continue

            if sid in same_name and entry is None:
                same = same_name[sid]
                item.update(
                    action="conflict",
                    kind="same-name-two-roots",
                    needs_action=True,
                    options=[e["root"] for e in same["entries"]],
                    detail="不同 root 下存在同名 skill 且内容不同；请选择保留哪个 root 的版本"
                    "（另一个 root 的目录不会被删除，只是不再参与同步）。",
                )
                items.append(item)
                continue

            if local_dir is not None:
                self._plan_with_local(
                    item, sid, entry, local_dir, local_files, local_hash, remote_dir, remote_files,
                    remote_hash, base_dir, base_hash, tomb, uninstalled, local_only, approval, ctx,
                )
            else:
                self._plan_without_local(
                    item, sid, remote_dir, remote_files, remote_hash, base_hash, tomb,
                    uninstalled, local_only, pref,
                )

            if sid in same_name and entry is not None:
                others = sorted({e["root"] for e in same_name[sid]["entries"]} - {entry["root"]})
                item["warnings"].append(
                    f"同名 skill 在 {entry['root']} 与其他 root（{'、'.join(others)}）下内容不同；"
                    f"本次只按已选定的 root={entry['root']} 处理，其他 root 的目录保持原样、"
                    "不参与同步也不会被删除。"
                )

            if item["action"] in ("push", "push_new", "adopt", "delete_local") and entry is not None:
                item["root"] = entry["root"]
                item["path"] = entry["path"]
            if item["action"] in ("deploy", "merge"):
                if item["action"] == "merge":
                    item["root"] = entry["root"]
                    item["path"] = str(local_dir)
                else:
                    root_key, target = self._target_for(sid, name)
                    item["root"], item["path"] = root_key, str(target) if target else ""
                    if not item["root"] or not item["path"]:
                        item.update(
                            action="error",
                            needs_action=True,
                            detail="找不到可用的安装根目录（检查配置 roots/default_root）。",
                        )
                    elif target and target.exists() and not target.is_dir():
                        item.update(
                            action="error",
                            needs_action=True,
                            detail=f"安装目标已存在且不是目录：{target}",
                        )
                    elif target and target.is_dir() and not base_hash:
                        item.update(
                            action="error",
                            needs_action=True,
                            detail=f"安装目标已存在但不是已识别的 skill，拒绝覆盖：{target}",
                        )
            self._set_expect(item, local_dir, local_hash)
            items.append(item)
        return items, ctx

    @staticmethod
    def _entry_input(entry: dict) -> dict:
        """冲突记录里的输入指纹（新记录平铺为 entry["input"]，兼容旧的 extra.input）。"""
        value = entry.get("input")
        if isinstance(value, dict):
            return value
        legacy = (entry.get("extra") or {}).get("input")
        return legacy if isinstance(legacy, dict) else {}

    @staticmethod
    def _entry_requested(entry: dict) -> str:
        requested = entry.get("requested")
        if not requested:
            requested = (entry.get("extra") or {}).get("requested")
        return str(requested or "")

    @staticmethod
    def _conflict_intent(entry: dict) -> str:
        """冲突的"意图"，与候选内容(candidates)分开建模，避免用内容反推语义。

        * ``global-delete``：本机有改动，同时收到一个尚未执行的全局删除请求
          （``requested=delete-shared``）。选"接受远端"= 批准执行这次删除，与 hub
          此刻是否还有内容无关——刷新出来的新冲突版本会把 hub 内容当证据填回候选。
        * ``remote-delete``：远端确实已经删除了该 skill（``delete-modify`` 且远端无
          候选内容）。选"接受远端"= 接受已经发生的删除，不写墓碑。
        * ``content``：内容层面的取舍（``first-import`` / ``content`` /
          ``same-name-two-roots``）。
        """
        if Engine._entry_requested(entry) == "delete-shared":
            return "global-delete"
        if entry.get("kind") == "delete-modify" and not (entry.get("candidates") or {}).get("remote"):
            return "remote-delete"
        return "content"

    @staticmethod
    def _conflict_input_matches(entry: dict, current: dict) -> bool:
        """冲突批准时记录的输入指纹是否与当前完全一致（旧版本记录退化为哈希比较）。"""
        recorded = Engine._entry_input(entry)
        if recorded:
            return recorded == current
        return (entry.get("local_hash") == current["local"]["hash"]
                and entry.get("remote_hash") == current["remote"]["hash"])

    def _set_expect(self, item: dict, local_dir: Path | None, local_hash: str) -> None:
        """记录"计划时本机这一版内容"，写入/删除前据此复核，防止覆盖计划后新改动。"""
        action = item["action"]
        if action == "deploy":
            planned_path = item.get("path") or ""
            exists = local_dir is not None
        elif action in ("merge", "delete_local", "push", "push_new", "adopt", "mark_uninstalled"):
            planned_path = str(local_dir) if local_dir else ""
            exists = local_dir is not None
        else:
            return
        item["expect"] = {
            "action": action,
            "path": planned_path,
            "exists": exists,
            "hash": local_hash if exists else "",
        }

    def _check_expect(self, expect: dict, actual: Path, what: str) -> None:
        """复核计划后本机内容未变化；有变化则停止（保留新内容，交由用户重算）。"""
        if not expect:
            return
        planned_path = expect.get("path") or ""
        if planned_path and os.path.normcase(str(actual)) != os.path.normcase(planned_path):
            raise ConflictPending(
                f"{what}目标与计划不一致（计划 {planned_path}，实际 {actual}），已停止。",
                hint="重新运行 `skills-sync plan` 或 `skills-sync sync` 重算。",
            )
        exists = actual.exists()
        if exists != bool(expect.get("exists")):
            raise ConflictPending(
                f"计划之后{what}的存在状态发生变化（现在{'存在' if exists else '不存在'}），"
                "已停止以避免覆盖/误删新内容。",
                hint="检查最新内容后重新运行 sync。",
                details={"path": str(actual)},
            )
        if not exists:
            return
        files = util.snapshot_dir(actual, exclude=self._excluder())
        current = util.tree_hash(files) if files else ""
        if current != (expect.get("hash") or ""):
            raise ConflictPending(
                f"计划之后{what}内容发生了变化，旧批准/计划不再适用，已停止以避免覆盖新改动。",
                hint="检查最新内容后重新运行 sync；旧冲突与备份均保留。",
                details={"path": str(actual)},
            )

    def _check_hub_unchanged(self, item: dict) -> None:
        """合并前复核共享库内容仍是计划时那一版（防止把远端新内容当旧版本合并）。"""
        planned = ((item.get("input") or {}).get("remote") or {}).get("hash") or ""
        dest = self.hub / "skills" / item["id"]
        current = ""
        if dest.is_dir():
            files = util.snapshot_dir(dest)
            current = util.tree_hash(files) if files else ""
        if current != planned:
            raise ConflictPending(
                f"共享库中 {item['id']!r} 的内容在计划后发生变化，已停止合并。",
                hint="重新运行 sync 会基于最新内容重算。",
            )

    def _pick_local(self, sid: str, entries: list[dict]) -> dict | None:
        """在多个 root 的同名目录里挑出"这一份本机副本"。

        优先级：显式偏好（uninstall/restore 记录）> 安装记录 > `include --root` 批准里
        记录的 root > 内容是否一致 > 默认 root。批准里带 root 时会被后续同步真正消费，
        因此 `include vendor --root agents` 之后 sync 处理的就是 agents 那一份。
        """
        if not entries:
            return None
        pref_root = self.state.pref(sid).get("preferred_root")
        install = self.state.install(sid)
        approved_root = (self._approvals().get(sid) or {}).get("root") or ""
        for candidate_root in (pref_root, (install or {}).get("root"), approved_root):
            if not candidate_root:
                continue
            for entry in entries:
                if entry["root"] == candidate_root:
                    return entry
        if len(entries) == 1:
            return entries[0]
        if len({e["hash"] for e in entries}) == 1:
            for entry in entries:
                if entry["root"] == self.cfg.default_root:
                    return entry
            return entries[0]
        return None

    def _plan_with_local(
        self, item, sid, entry, local_dir, local_files, local_hash, remote_dir, remote_files,
        remote_hash, base_dir, base_hash, tomb, uninstalled, local_only, approval, ctx,
    ) -> None:
        if local_only:
            item.update(action="skip_local_only", detail="已标记为仅本机，不参与同步。")
            if remote_dir is not None and remote_hash != local_hash:
                item["warnings"].append("共享库中仍有旧版本副本；本机改动不会传播。")
            return
        if uninstalled:
            item.update(action="skip_uninstalled", detail="本机已卸载，不自动装回。")
            if local_hash != base_hash:
                item["warnings"].append("目录内容与基线不同（可能手工恢复过）；不会传播，需 restore 后才会同步。")
            return
        if tomb:
            if base_hash and local_hash == base_hash:
                item.update(action="delete_local", detail="其他设备已全局删除，本机副本未被改动，随删除一起移除。")
            else:
                item.update(
                    action="conflict",
                    kind="delete-modify",
                    needs_action=True,
                    options=["local", "remote"],
                    detail="共享库已全局删除该 skill，但本机仍存在（且与基线不同或没有基线）；"
                    "请选择保留本机版本（重新纳入）或接受删除。",
                )
            return
        if remote_dir is None:
            if not base_hash:
                if approval.get("hash") == local_hash:
                    item.update(action="push_new", detail="已批准的新 skill，将纳入共享库。")
                else:
                    detail = "本机新增，尚未纳入共享库；批准后才会推送。"
                    if approval:
                        detail = "批准已失效（内容在批准后发生变化），需要重新审批。"
                    item.update(action="pending_add", needs_action=True, detail=detail)
                return
            item.update(
                action="push",
                detail="共享库中缺少该 skill 副本；按本机基线/内容补回。",
            )
            item["warnings"].append("没有删除墓碑却缺失共享副本，可能是仓库内容丢失；已按本机内容补回。")
            return

        if not base_hash:
            if local_hash == remote_hash:
                item.update(action="adopt", detail="本机与共享库内容相同，仅补齐基线。")
            else:
                item.update(
                    action="conflict",
                    kind="first-import",
                    needs_action=True,
                    options=["local", "remote"],
                    detail="首次汇总：共享库已存在同名 skill 且内容不同，不能按时间覆盖；请选择保留哪一端。",
                )
            return

        if local_hash == base_hash and remote_hash == base_hash:
            item.update(action="unchanged")
            return
        if local_hash == remote_hash:
            item.update(action="unchanged", detail="两端内容一致。")
            return
        if remote_hash == base_hash:
            item.update(action="push", detail="仅本机有改动，将推送到共享库。")
            return
        if local_hash == base_hash:
            item.update(action="deploy", detail="仅共享库有改动，将更新本机。")
            return

        merged = merge.plan_tree_merge(
            base_dir=base_dir,
            local_dir=local_dir,
            remote_dir=remote_dir,
            base_files=ctx[sid]["base_files"],
            local_files=local_files,
            remote_files=remote_files,
        )
        ctx[sid]["merge"] = merged
        if merged["status"] == "clean":
            item.update(action="merge", detail=f"两端各自改动可自动合并（{len(merged['ops'])} 个文件操作）。")
        else:
            item.update(
                action="conflict",
                kind="content",
                needs_action=True,
                options=["local", "remote", "merged"],
                detail="两端修改了同一区域或二进制文件，无法自动合并："
                + "；".join(f"{c['path']}（{c['kind']}）" for c in merged["conflicts"][:6]),
            )
            item["conflict_files"] = merged["conflicts"][:MAX_DIFF_FILES]

    def _plan_without_local(
        self, item, sid, remote_dir, remote_files, remote_hash, base_hash, tomb, uninstalled,
        local_only, pref,
    ) -> None:
        if remote_dir is None:
            if tomb:
                item.update(action="unchanged", detail="已全局删除（本机也没有副本）。")
            elif base_hash:
                item.update(action="unchanged", detail="本机与共享库都不存在该 skill。")
                item["warnings"].append("保留旧基线供排查；如确认不再需要可运行 `skills-sync recover --prune-baseline`。")
            return
        if uninstalled or local_only:
            item.update(action="skip_uninstalled", detail="本机标记为不安装 / 仅本机，不自动部署。")
            return
        if base_hash:
            item.update(
                action="mark_uninstalled",
                detail="本机目录缺失（视为本机卸载），不会传播删除；已记录不安装清单。",
            )
            return
        item.update(action="deploy", detail="共享库新增 skill，将部署到本机。")

    def _target_for(self, sid: str, name: str) -> tuple[str, Path | None]:
        install = self.state.install(sid)
        if install and install.get("root") and self.cfg.root_by_key(install["root"]):
            return install["root"], Path(install["path"])
        root_key = self.state.pref(sid).get("preferred_root") or self.cfg.default_root
        root = self.cfg.root_by_key(root_key)
        if not root:
            return "", None
        return root_key, Path(root["path"]) / safe_dir_name(name, sid)

    def _compat_preview(self, local_scan: dict) -> list[dict]:
        out = []
        total = 0
        for entry in sorted(local_scan["skills"].values(), key=lambda e: e["id"]):
            if not entry["compat"]:
                continue
            out.append({
                "id": entry["id"],
                "root": entry["root"],
                "findings": entry["compat"][:20],
            })
            total += len(entry["compat"])
            if total > MAX_COMPAT_PREVIEW:
                break
        return out

    def _plan_warnings(self, items: list[dict], local_scan: dict) -> list[str]:
        warnings = []
        for item in items:
            for text in item.get("warnings", []):
                warnings.append(f"{item['id']}：{text}")
        if local_scan["filter_report"]["secret_files"]:
            warnings.append(
                f"有 {len(local_scan['filter_report']['secret_files'])} 个疑似凭据文件被排除，"
                "不会进入同步包；请确认 skill 不依赖这些文件。"
            )
        if local_scan["filter_report"]["excluded_dirs"]:
            warnings.append(
                f"有 {len(local_scan['filter_report']['excluded_dirs'])} 个缓存/产物条目被排除。"
            )
        if any(e.get("links") for e in local_scan["skills"].values()):
            warnings.append("部分 skill 内含符号链接；链接本身不纳入同步（不会跟随到根目录之外）。")
        ok = {r["key"] for r in self.cfg.roots}
        if self.cfg.default_root not in ok:
            warnings.append(f"default_root={self.cfg.default_root!r} 无效。")
        return warnings

    # ------------------------------------------------------------------ 同步

    def sync(self, *, dry_run: bool = False) -> dict:
        if dry_run:
            data = self.plan(fetch=True)
            data["dry_run"] = True
            return data
        self.state.lock()
        op = self.state.new_op("sync", {"remote": util.redact_url(self.cfg.remote_url)})
        try:
            result = self._sync(op)
            if result.get("failures"):
                status = STATUS_PARTIAL
            elif result.get("needs_action"):
                status = STATUS_NEEDS_ACTION
            else:
                status = STATUS_OK
            result["status"] = status
            self.state.finish_op(op, status, {
                "pushed": result["pushed"],
                "deployed": result["deployed"],
                "conflicts": [c["skill_id"] for c in result["conflicts"]],
                "stale": [s["skill_id"] for s in result["stale"]],
                "pending": result["pending"],
                "failures": [f["skill_id"] for f in result["failures"]],
            })
            self.state.save()
            return result
        except SyncError as exc:
            status = STATUS_PARTIAL if _op_has_progress(op) else STATUS_FAILED
            self.state.finish_op(op, status, {"error": exc.message, "hint": exc.hint})
            self.state.save()
            raise
        finally:
            self.state.unlock()

    def _push_if_ahead(self, op: dict) -> bool:
        """本地领先远端时补推（上次推送失败的本地提交），返回是否推送过。"""
        if not self.cfg.remote_url or not self._hub_ready():
            return False
        local = self.git.head(self.hub)
        remote = self.git.remote_head(self.hub, self.cfg.branch)
        if not local or not remote or local == remote:
            return False
        if not self.git.is_ancestor(self.hub, remote, local):
            # 远端有本地没有的提交：交给下次 fetch 后的重算/合并，不能强推
            return False
        self._fault("push")
        self.git.push(self.hub)
        op["steps"].append({"at": util.now_iso(), "phase": "pushed-after-retry",
                            "status": "ok", "commit": local})
        self.state.save_op(op)
        return True

    def _sync(self, op: dict) -> dict:
        hub = self.hub
        self._prepare_hub(fetch=True, op=op)
        op["steps"].append({"at": util.now_iso(), "phase": "hub-ready", "status": "ok"})
        self.state.save_op(op)

        items, ctx = [], {}
        stale: dict[str, dict] = {}
        for attempt in range(1, 4):
            items, ctx = self._build_plan(
                scan.scan_roots(self.cfg),
                scan.scan_hub(hub) if self._hub_ready() else {},
                self._load_manifest() if self._hub_ready() else manifest.empty(),
            )
            self._record_pending(items, ctx)
            changes = [i for i in items if i["action"] in ("push", "push_new", "merge")]
            if not changes:
                break
            written, write_stale = self._write_hub(changes, ctx, op)
            for entry in write_stale:
                stale[entry["skill_id"]] = entry
            self._update_manifest(written, ctx)
            commit = self.git.commit_all(hub, self._commit_message(op, changes))
            if not commit:
                break
            if not self.cfg.remote_url:
                break
            self._fault("push")
            try:
                self.git.push(hub)
            except GitError as exc:
                if not self._push_retryable(exc) or attempt == 3:
                    raise
                op["steps"].append({
                    "at": util.now_iso(),
                    "phase": f"push-retry-{attempt}",
                    "status": "retry",
                    "detail": exc.message,
                })
                self.state.save_op(op)
                self._fault("fetch")
                self.git.fetch(hub)
                self._reset_worktree(op)
                continue
            op["steps"].append({"at": util.now_iso(), "phase": "pushed", "status": "ok", "commit": commit})
            self.state.save_op(op)
            break

        self._push_if_ahead(op)

        result = {
            "op_id": op["op_id"],
            "device": self.cfg.device_name,
            "hub": str(hub),
            "remote_url": util.redact_url(self.cfg.remote_url),
            "branch": self.cfg.branch,
            "head": self.git.head(hub) if self._hub_ready() else None,
            "pushed": [],
            "deployed": [],
            "merged": [],
            "adopted": [],
            "deleted_local": [],
            "marked_uninstalled": [],
            "pending": [],
            "conflicts": [],
            "stale": [],
            "skipped": [],
            "errors": [],
            "failures": [],
            "notices": [],
            "plan_summary": _summarize(items),
        }

        for item in items:
            if item["action"] != "conflict":
                continue
            entry = self._ensure_conflict(item, ctx[item["id"]], op)
            item["conflict_id"] = entry["conflict_id"] if entry else ""
            result["conflicts"].append({
                "skill_id": item["id"],
                "conflict_id": item.get("conflict_id", ""),
                "kind": item["kind"],
                "detail": item["detail"],
                "options": item["options"],
                "paths": {k: v for k, v in (entry or {}).get("paths", {}).items() if k in ("diff",)},
            })

        for item in items:
            sid = item["id"]
            action = item["action"]
            if sid in stale:
                result["stale"].append(stale[sid])
                continue
            try:
                if action in ("deploy", "merge"):
                    self._deploy(item, op)
                    result["deployed"].append(sid)
                    if action == "merge":
                        result["merged"].append(sid)
                elif action in ("push", "push_new"):
                    self._baseline_from_hub(sid, item, op)
                    result["pushed"].append(sid)
                elif action == "adopt":
                    self._adopt(item, ctx[sid], op)
                    result["adopted"].append(sid)
                elif action == "delete_local":
                    self._delete_local(item, ctx[sid], op, reason="global-delete")
                    result["deleted_local"].append(sid)
                elif action == "mark_uninstalled":
                    self.state.set_pref(sid, uninstalled=True, uninstalled_at=util.now_iso(),
                                         reason="missing-local")
                    result["marked_uninstalled"].append(sid)
                elif action == "pending_add":
                    result["pending"].append(sid)
                elif action == "error":
                    result["errors"].append({"skill_id": sid, "detail": item["detail"]})
                elif action in ("skip_local_only", "skip_uninstalled"):
                    result["skipped"].append({"skill_id": sid, "action": action,
                                              "detail": item["detail"]})
            except ConflictPending as exc:
                # 执行阶段发现计划后内容变化：保留新内容，不覆盖，交由用户重算
                result["stale"].append({
                    "skill_id": sid, "action": action, "phase": "apply",
                    "message": exc.message, "hint": exc.hint,
                })
                op["skills"][sid] = {"status": "stale", "action": action,
                                     "message": exc.message, "hint": exc.hint,
                                     "at": util.now_iso()}
                self.state.save_op(op)
                self.state.save()
            except (DeployError, OSError, StateError) as exc:
                message = getattr(exc, "message", str(exc))
                hint = getattr(exc, "hint", None)
                result["failures"].append({"skill_id": sid, "message": message, "hint": hint})
                op["skills"][sid] = {"status": "failed", "action": action,
                                     "message": message, "hint": hint, "at": util.now_iso()}
                self.state.save_op(op)
                self.state.save()

        if result["pending"]:
            result["needs_action"] = True
            result["notices"].append("有新增 skill 等待批准：`skills-sync plan` 查看后 `skills-sync include <id>`。")
        if result["conflicts"]:
            result["needs_action"] = True
            result["notices"].append("存在冲突：`skills-sync resolve <id> --take local|remote` 后 `skills-sync continue`。")
        if result["errors"]:
            result["needs_action"] = True
        if result["stale"]:
            result["needs_action"] = True
            result["notices"].append(
                "同步过程中检测到计划后又有改动：已保留新内容、未覆盖；"
                "请重新运行 `skills-sync sync` 让工具按最新内容重算。"
            )
        if self.cfg.remote_url and not result["pushed"] and not result["deployed"] and not result["failures"]:
            result["notices"].append("本次没有需要传输的改动。")
        return result

    # ------------------------------------------------------------------ 写共享库

    def _record_pending(self, items: list[dict], ctx: dict) -> None:
        """把"本机新增、尚未批准"的内容登记为待审批；已进入共享库的清掉旧记录。"""
        changed = False
        for item in items:
            sid = item["id"]
            if item["action"] == "pending_add":
                entry = ctx[sid].get("local")
                if not entry:
                    continue
                self.state.add_pending_addition(
                    sid,
                    name=entry["dir_name"],
                    files=entry["files"],
                    src_dir=Path(entry["path"]),
                    roots=[entry["root"]],
                    source=entry.get("source") or {},
                    flags=[],
                )
                changed = True
            elif item["action"] in ("push", "push_new", "merge", "adopt"):
                if self.state.pending_addition(sid):
                    self.state.clear_pending(sid)
                    changed = True
        if changed:
            self.state.save()

    def _write_hub(self, changes: list[dict], ctx: dict, op: dict) -> tuple[dict, list[dict]]:
        (self.hub / "skills").mkdir(parents=True, exist_ok=True)
        written: dict[str, dict] = {}
        stale: list[dict] = []
        for item in changes:
            try:
                written[item["id"]] = self._write_one_hub(item, ctx, op)
            except ConflictPending as exc:
                # 计划后内容变化：保留新内容，不写共享库，交由用户重算
                stale.append({"skill_id": item["id"], "action": item["action"],
                              "phase": "write-hub", "message": exc.message, "hint": exc.hint})
                op["skills"][item["id"]] = {
                    "status": "stale", "action": item["action"], "message": exc.message,
                    "hint": exc.hint, "at": util.now_iso(),
                }
                self.state.save_op(op)
        return written, stale

    def _write_one_hub(self, item: dict, ctx: dict, op: dict) -> dict:
        sid = item["id"]
        dest = self.hub / "skills" / sid
        tmp = self._tmp_dir(op, f"hub-{sid}")
        if item["action"] == "merge":
            self._check_expect(item.get("expect") or {}, Path(item["path"]), "待合并的本机目录")
            self._check_hub_unchanged(item)
            self._materialize_merge(ctx[sid], tmp)
        else:
            entry = ctx[sid]["local"]
            if not entry:
                raise StateError(f"skill {sid!r} 缺少本机内容，无法推送。")
            self._check_expect(item.get("expect") or {}, Path(entry["path"]), "待推送的本机目录")
            util.copy_selected(Path(entry["path"]), entry["files"], tmp)
        files = util.snapshot_dir(tmp)
        digest = util.tree_hash(files)
        if not files:
            raise StateError(f"skill {sid!r} 过滤后为空，拒绝推送。")
        self._fault("hub-write")
        self._replace_dir(tmp, dest)
        op["skills"][sid] = {"status": "pushed", "hash": digest, "files": len(files),
                             "at": util.now_iso()}
        self.state.save_op(op)
        return {
            "files": files,
            "hash": digest,
            "path": dest,
            "item": item,
            "entry": ctx[sid]["local"],
        }

    def _materialize_merge(self, context: dict, dest: Path) -> None:
        plan = context.get("merge")
        if not plan:
            raise StateError("内部错误：缺少合并计划。")
        local_dir = context["local_dir"]
        remote_dir = context["remote_dir"]
        ops = {op["path"]: op for op in plan["ops"]}
        for rel, meta in plan["files"].items():
            if not util.safe_relpath(rel):
                raise StateError(f"合并结果包含非法路径：{rel!r}")
            target = dest / Path(rel)
            op = ops.get(rel)
            if op and op["action"] == "write":
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(op["data"])
            elif op and op["action"] == "copy_remote":
                util.copy_file(remote_dir / rel, target, source_root=remote_dir, target_root=dest)
            else:
                util.copy_file(local_dir / rel, target, source_root=local_dir, target_root=dest)

    def _require_target_version(self, target: Path, expected: dict) -> None:
        """Compare the whole local directory, including local-only files."""
        exists = target.exists() or util.is_link_like(target)
        if exists != expected["exists"] or (exists and (
            util.is_link_like(target) or not target.is_dir()
            or not util.tree_equals(util.snapshot_dir(target), expected["files"])
        )):
            raise ConflictPending(
                "目标在备份或恢复准备期间又发生变化，已保留新内容并停止替换。",
                hint="查看当前内容后重新计算同步/恢复计划；不会用旧副本覆盖。",
                details={"path": str(target), "phase": "before-replace"},
            )

    def _replace_dir(self, source: Path, target: Path, *, expected: dict | None = None) -> None:
        """用 source 替换 target，绝不"先删旧目录"。

        旧目录先挪到同目录下的临时名，成功换上新内容后才删除；换新失败则把旧目录
        挪回原位。跨盘/占用导致 rename 失败时退回"先完整复制、再删旧目录"，复制失败
        同样把旧目录挪回，保证目标位置不会出现"新旧都没有"的空档。
        """
        target.parent.mkdir(parents=True, exist_ok=True)
        util.guard_path(target.parent, target, "替换目标")
        if expected is not None:
            self._require_target_version(target, expected)
        old: Path | None = None
        if target.exists() or util.is_link_like(target):
            old = target.with_name(f".{target.name}.ss-old-{os.getpid()}-{uuid.uuid4().hex[:8]}")
            os.replace(str(target), str(old))
        try:
            # Verify the directory actually detached, rather than discarding an
            # edit arriving after the pre-rename check. On mismatch the except
            # branch moves it back without deploying anything.
            if expected is not None:
                self._require_target_version(old if old is not None else target, expected)
            try:
                os.replace(str(source), str(target))
            except OSError:
                util.copy_tree(source, target)
                util.remove_dir(source)
        except BaseException:
            if target.exists() or util.is_link_like(target):
                try:
                    util.remove_dir(target)
                except OSError:
                    pass
            if old is not None:
                os.replace(str(old), str(target))
            raise
        if old is not None:
            try:
                util.remove_dir(old)
            except OSError:
                pass

    def _remove_dir_versioned(self, target: Path, expected: dict) -> None:
        """Detach then verify the exact directory before removing it."""
        util.guard_path(target.parent, target, "删除目标")
        self._require_target_version(target, expected)
        held = target.with_name(f".{target.name}.ss-delete-{uuid.uuid4().hex[:8]}")
        os.replace(str(target), str(held))
        try:
            self._require_target_version(held, expected)
            if target.exists() or util.is_link_like(target):
                raise ConflictPending("删除期间目标被重新创建，已保留两个版本。",
                                      details={"path": str(target), "retained": str(held)})
            util.remove_dir(held)
        except BaseException:
            if held.exists() and not target.exists() and not util.is_link_like(target):
                os.replace(str(held), str(target))
            raise

    def _update_manifest(self, written: dict, ctx: dict) -> None:
        man = self._load_manifest()
        for sid, info in written.items():
            entry = info.get("entry") or {}
            manifest.upsert(
                man,
                sid,
                name=info["item"]["name"],
                files=info["files"],
                content_hash=info["hash"],
                source=entry.get("source"),
                licenses=entry.get("licenses"),
            )
        manifest.save(self.hub, man)

    def _commit_message(self, op: dict, changes: list[dict]) -> str:
        names = ", ".join(sorted(i["id"] for i in changes)[:6])
        return (
            f"skills-sync: {self.cfg.device_name} sync {len(changes)} skill(s)"
            f" [{names}] ({op['op_id']})"
        )

    def _push_retryable(self, exc: GitError) -> bool:
        text = (exc.message + " " + str(exc.details.get("stderr", ""))).lower()
        for key in ("rejected", "non-fast-forward", "fetch first", "stale info", "failed to push"):
            if key in text:
                return True
        return False

    # ------------------------------------------------------------------ 部署

    def _stage_and_swap(self, *, sid: str, source: Path, files: dict, digest: str,
                        root_key: str, target: Path, expect: dict | None, what: str,
                        op: dict, tag: str, commit=None,
                        pushed: list[str] | None = None) -> None:
        """统一安装/替换入口：准备副本 → 校验版本 → 替换前复核 → 替换 → 校验 → 失败回滚。

        `files`/`digest` 是"这次要落地的完整目标版本"。所有写入本机目录的入口
        （普通部署、自动合并、continue 的内容解决、本机 restore、备份恢复）都走这里，
        因此共享同一套保护：

        * 替换前把目标旧内容登记进备份，`recover --restore` 可以回退；
        * 先在目标之外准备副本并校验成 `files`，只有校验通过的副本才允许落地；
        * 准备副本期间本机目录若被编辑，替换前复核一定发现并停止，不覆盖新内容；
        * 替换本身不先删旧目录（见 `_replace_dir`），替换或写后校验失败按备份回滚。
        """
        if target.exists() and not target.is_dir():
            raise DeployError(f"{what}不是目录：{target}")
        expect = expect or {}
        # 计划/批准时那一版本机内容：第一道复核（快速失败，不浪费准备时间）
        self._check_expect(expect, target, what)
        existed = target.exists()
        before_files = util.snapshot_dir(target) if existed else {}
        before_hash = util.tree_hash(before_files) if before_files else ""
        staging = self._tmp_dir(op, tag)
        util.copy_tree(source, staging)
        staged, staged_hash = util.snapshot_hash_dir(staging)
        if files and staged != files:
            util.remove_dir(staging)
            raise DeployError(
                f"{what}的待安装内容在准备期间发生变化，已停止（本机目录未改动）。",
                hint="重新运行 `skills-sync sync` 让工具按最新内容重算。",
                details={"skill_id": sid, "expected": digest, "staged": staged_hash},
            )
        # 第二道复核：准备副本之后、真正替换之前。这段时间里的本机编辑必须保住。
        now_files = util.snapshot_dir(target) if target.exists() else {}
        now_hash = util.tree_hash(now_files) if now_files else ""
        if now_hash != before_hash:
            util.remove_dir(staging)
            raise ConflictPending(
                f"{what}在准备安装期间被修改，已停止部署并保留本机内容。",
                hint="确认改动后重新运行 `skills-sync sync`；本机内容未被覆盖。",
                details={"skill_id": sid, "path": str(target), "phase": "before-replace",
                         "pushed": list(pushed or [])},
            )
        self._check_expect(expect, target, what)
        self.state.backup_skill(
            op["op_id"], sid, root=root_key, install_path=target, existed=existed,
            files=before_files, hash_before=before_hash,
        )
        self._fault("deploy-write")
        try:
            # backup_skill may take time; validate again after it completes.
            self._replace_dir(staging, target,
                              expected={"exists": existed, "files": before_files})
            problems = util.verify_snapshot(target, files)
            if problems:
                raise DeployError(
                    f"{what}写入后校验失败：" + "；".join(problems[:5]),
                    details={"skill_id": sid, "problems": problems[:20]},
                )
            if commit is not None:
                commit()
        except ConflictPending:
            # _replace_dir already preserved/restored the newly edited target.
            # Rolling it back from the earlier backup would destroy that edit.
            if staging.exists():
                util.remove_dir(staging)
            raise
        except BaseException as exc:
            if not self._restore_backup(op["op_id"], sid, target):
                raise DeployError(
                    f"{what}失败且回滚未完成：{getattr(exc, 'message', exc)}",
                    hint="备份已保留；用 `skills-sync recover` 查看，"
                         f"或 `skills-sync recover --restore {op['op_id']}/{sid}` 重试。",
                    details={"skill_id": sid, "target": str(target), "rollback": "failed"},
                ) from exc
            raise

    def _deploy(self, item: dict, op: dict) -> None:
        sid = item["id"]
        src = self.hub / "skills" / sid
        if not src.is_dir():
            raise DeployError(f"共享库中缺少 {sid!r} 的内容，无法部署。")
        files, digest = util.snapshot_hash_dir(src)
        if not files:
            raise DeployError(f"共享库中 {sid!r} 为空，拒绝部署。")
        root = self.cfg.root_by_key(item["root"])
        if not root:
            raise DeployError(f"安装根 {item['root']!r} 不在配置中。")
        target = Path(item["path"])
        util.require_within(target, Path(root["path"]), "部署目标")
        if util.same_path(target, Path(root["path"])):
            raise DeployError("拒绝把 skill 部署到 root 目录本身。")

        def commit() -> None:
            self.state.set_baseline(
                sid, files=files, content_dir=src,
                commit=self.git.head(self.hub) or "", root=root["key"],
                install_path=str(target),
            )
            self.state.set_install(sid, root["key"], str(target))
            self.state.set_pref(sid, uninstalled=False, uninstalled_at=None,
                                preferred_root=root["key"])
            self.state.save()

        self._stage_and_swap(
            sid=sid, source=src, files=files, digest=digest, root_key=root["key"],
            target=target, expect=item.get("expect") or {}, what="部署目标",
            op=op, tag=f"deploy-{sid}", commit=commit,
        )
        op["skills"][sid] = {"status": "deployed", "path": str(target), "files": len(files),
                             "at": util.now_iso()}
        self.state.save_op(op)

    def _install_content(self, sid: str, files: dict, content_dir: Path, root_key: str,
                         target: Path, op: dict, *, expect: dict | None = None,
                         pushed: list[str] | None = None) -> None:
        """把已就绪目录安装到本机（备份、版本校验、替换前复核、回滚），并写基线。

        `expect` 是"这次批准所依据的本机版本"：内容解决/恢复都必须传，才能在
        推送完成到落地之间发现本机新改动并保住它。
        """
        root = self.cfg.root_by_key(root_key)
        if not root:
            raise DeployError(f"安装根 {root_key!r} 不在配置中。")
        util.require_within(target, Path(root["path"]), "部署目标")
        digest = util.tree_hash(files) if files else ""

        def commit() -> None:
            self.state.set_baseline(
                sid, files=files, content_dir=content_dir,
                commit=self.git.head(self.hub) or "", root=root_key,
                install_path=str(target),
            )
            self.state.set_install(sid, root_key, str(target))
            self.state.save()

        self._stage_and_swap(
            sid=sid, source=content_dir, files=files, digest=digest, root_key=root_key,
            target=target, expect=expect or {}, what="安装目标", op=op,
            tag=f"install-{sid}", commit=commit, pushed=pushed,
        )

    def _baseline_from_hub(self, sid: str, item: dict, op: dict) -> None:
        src = self.hub / "skills" / sid
        files, _ = util.snapshot_hash_dir(src)
        if not files:
            raise StateError(f"共享库中 {sid!r} 内容为空，拒绝写入空基线。")
        root_key = item["root"] or self.state.pref(sid).get("preferred_root") or self.cfg.default_root
        install = self.state.install(sid)
        target = (install or {}).get("path") or item["path"]
        if not target:
            root = self.cfg.root_by_key(root_key)
            target = str(Path(root["path"]) / safe_dir_name(item["name"], sid)) if root else ""
        if not target:
            return
        self.state.set_baseline(
            sid, files=files, content_dir=src,
            commit=self.git.head(self.hub) or "", root=root_key, install_path=target,
        )
        self.state.set_install(sid, root_key, target)
        self.state.save()

    def _adopt(self, item: dict, context: dict, op: dict) -> None:
        sid = item["id"]
        src = self.hub / "skills" / sid
        files, digest = util.snapshot_hash_dir(src)
        local_dir = context["local_dir"]
        if local_dir is None:
            raise StateError(f"skill {sid!r} 本机内容缺失，无法补齐基线。")
        problems = util.verify_snapshot(local_dir, files, exclude=self._excluder())
        if problems:
            raise StateError(f"skill {sid!r} 与本机内容不一致：" + "；".join(problems[:5]))
        self.state.set_baseline(
            sid, files=files, content_dir=src, commit=self.git.head(self.hub) or "",
            root=item["root"] or context["local"]["root"], install_path=str(local_dir),
        )
        self.state.set_install(sid, context["local"]["root"], str(local_dir))
        self.state.save()

    def _delete_local(self, item: dict, context: dict, op: dict, *, reason: str) -> None:
        sid = item["id"]
        local_dir = context.get("local_dir")
        if local_dir is not None and local_dir.exists():
            # 删除前复核：计划/批准后又被编辑过则保留新内容，拒绝删除
            if item.get("expect"):
                self._check_expect(item["expect"], Path(local_dir), "待删除的本机目录")
            full_before = util.snapshot_dir(local_dir)
            files = util.snapshot_dir(local_dir, exclude=self._excluder())
            self.state.backup_skill(
                op["op_id"], sid, root=context["local"]["root"], install_path=local_dir,
                existed=True, files=files, hash_before=util.tree_hash(files),
            )
            self._remove_dir_versioned(Path(local_dir), {"exists": True, "files": full_before})
        self.state.clear_baseline(sid)
        self.state.data["install_map"].pop(sid, None)
        self.state.set_pref(sid, uninstalled=True, deleted_shared=(reason == "global-delete"),
                            uninstalled_at=util.now_iso(), reason=reason)
        self.state.save()

    def _restore_backup(self, op_id: str, sid: str, target: Path) -> bool:
        """把备份内容放回目标；成功返回 True。

        与部署共用同一套安全语义：先在目标之外准备副本、校验备份完整性，只有校验
        通过的副本才替换目标。准备阶段失败时当前目录原样保留——不会出现"目标已被
        删掉、备份又没复制回来"的中间状态。替换本身失败时旧目录会被挪回（`_replace_dir`）。
        """
        meta = self.state.read_backup(op_id, sid)
        if not meta:
            return False
        backup = self.state.backup_dir(op_id, sid) / "files"
        target_exists = target.exists() or util.is_link_like(target)
        target_files = util.snapshot_dir(target) if target_exists else {}
        existed = bool(meta.get("existed"))
        if existed and not backup.is_dir():
            return False
        expected: dict = {}
        if existed:
            expected = util.snapshot_dir(backup)
            if not expected:
                return False
            # Some legacy backups include excluded local files as well. Every
            # recorded file must still match; additional local files are kept.
            for rel, recorded in (meta.get("files") or {}).items():
                if rel not in expected or expected[rel].get("hash") != recorded.get("hash"):
                    return False
        staging = self._tmp_dir({"op_id": op_id}, f"restore-{sid}")
        try:
            if existed:
                util.copy_tree(backup, staging)
                problems = util.verify_snapshot(staging, expected)
                if problems:
                    util.remove_dir(staging)
                    return False
            else:
                staging.mkdir(parents=True, exist_ok=True)
            try:
                self._replace_dir(staging, target,
                                  expected={"exists": target_exists, "files": target_files})
            except OSError:
                return False
            if existed:
                problems = util.verify_snapshot(target, expected)
                if problems:
                    return False
            return True
        except OSError:
            if staging.exists():
                try:
                    util.remove_dir(staging)
                except OSError:
                    pass
            return False

    # ------------------------------------------------------------------ 冲突落盘

    def _ensure_conflict(self, item: dict, context: dict, op: dict) -> dict | None:
        existing = context.get("existing")
        superseded = item.get("supersedes") or ""
        if existing and not superseded:
            return existing
        sid = item["id"]
        kind = item["kind"]
        if kind == "same-name-two-roots":
            same = context.get("same_name") or {"entries": []}
            entries = same["entries"]
            candidates = {e["root"]: e["path"] for e in entries}
            hash_map = {e["root"]: e["hash"] for e in entries}
            dirs = []
            for entry in entries[:2]:
                tmp = self._tmp_dir(op, f"cand-{sid}-{entry['root']}")
                util.copy_selected(Path(entry["path"]), self._entry_files(entry), tmp)
                dirs.append(tmp)
            while len(dirs) < 2:
                dirs.append(dirs[0])
            local_dir, remote_dir = dirs[0], dirs[1]
            local_files = util.snapshot_dir(local_dir)
            remote_files = util.snapshot_dir(remote_dir)
            base_files: dict = {}
            base_dir = None
            options = [e["root"] for e in entries]
            extra = {"candidates": candidates, "candidate_hashes": hash_map}
            local_hash = item["local_hash"] or util.tree_hash(local_files)
            remote_hash = item["remote_hash"] or util.tree_hash(remote_files)
            base_hash = ""
        else:
            local_dir = context.get("local_dir")
            remote_dir = context.get("remote_dir")
            base_dir = context.get("base_dir")
            local_files = context.get("local_files") or {}
            remote_files = context.get("remote_files") or {}
            base_files = context.get("base_files") or {}
            options = ["local", "remote"]
            if item["kind"] == "content":
                options = ["local", "remote", "merged"]
            # 候选目录只保存过滤后的内容
            local_dir = self._clean_candidate(local_dir, local_files, op, f"cand-{sid}-local")
            remote_dir = self._clean_candidate(remote_dir, remote_files, op, f"cand-{sid}-remote")
            extra = {"candidates": {
                "local": str(local_dir) if local_dir else "",
                "remote": str(remote_dir) if remote_dir else "",
            }}
            local_hash = item["local_hash"]
            remote_hash = item["remote_hash"]
            base_hash = item["base_hash"]

        # 版本绑定：记录冲突创建时的完整输入指纹，批准只对这一版有效
        extra["input"] = item.get("input") or {}
        if superseded:
            extra["supersedes"] = superseded
            old = self.state.get_conflict(sid) or {}
            if self._entry_requested(old):
                extra["requested"] = self._entry_requested(old)

        diff_text = ""
        merge_plan = context.get("merge")
        if merge_plan:
            diff_text = merge_plan.get("diff") or ""
        elif local_dir is not None and remote_dir is not None:
            files = [rel for rel in sorted(set(local_files) | set(remote_files))
                     if (local_files.get(rel) or {}).get("hash") != (remote_files.get(rel) or {}).get("hash")]
            conflicts = [{"path": rel, "kind": kind, "detail": item["detail"]}
                         for rel in files[:MAX_DIFF_FILES]]
            diff_text = merge.build_diff(local_dir, remote_dir, local_files, remote_files, conflicts)
            if len(files) > MAX_DIFF_FILES:
                diff_text += f"\n（仅显示前 {MAX_DIFF_FILES} 个不同文件，共 {len(files)} 个）\n"

        entry = self.state.add_conflict(
            sid,
            kind=kind,
            local_dir=local_dir,
            remote_dir=remote_dir,
            base_dir=base_dir,
            local_files=local_files,
            remote_files=remote_files,
            base_files=base_files,
            local_hash=local_hash,
            remote_hash=remote_hash,
            base_hash=base_hash,
            options=options,
            detail=item["detail"],
            diff_text=diff_text,
            extra=extra,
        )
        self.state.save()
        return entry

    def _entry_files(self, entry: dict) -> dict:
        return entry.get("files") or {}

    def _clean_candidate(self, src: Path | None, files: dict, op: dict, tag: str) -> Path | None:
        if src is None:
            return None
        tmp = self._tmp_dir(op, tag)
        util.copy_selected(src, files, tmp)
        return tmp

    # ------------------------------------------------------------------ 审批

    def include(self, ids: list[str], *, all_pending: bool = False, exclude: bool = False,
                root: str = "") -> dict:
        for sid in ids:
            _validate_skill_id(sid)
        local_scan = scan.scan_roots(self.cfg)
        by_id: dict[str, list[dict]] = {}
        for entry in local_scan["skills"].values():
            by_id.setdefault(entry["id"], []).append(entry)
        if root:
            keys = [r["key"] for r in self.cfg.roots]
            if root not in keys:
                raise UsageError(f"未知的 root：{root!r}", hint="可用 root：" + ", ".join(keys))
            filtered = {sid: [e for e in entries if e["root"] == root]
                        for sid, entries in by_id.items()}
            by_id = {sid: entries for sid, entries in filtered.items() if entries}
        if all_pending:
            known_remote = set(scan.scan_hub(self.hub)) if self._hub_ready() else set()
            ids = sorted(sid for sid in by_id if sid not in known_remote)
        if not ids:
            raise UsageError("没有指定 skill；用 `skills-sync plan` 查看待批准清单。")
        self.state.lock()
        try:
            return self._include_locked(ids, by_id, exclude)
        finally:
            self.state.unlock()

    def _include_locked(self, ids: list[str], by_id: dict[str, list[dict]], exclude: bool) -> dict:
        results = []
        for sid in ids:
            entries = by_id.get(sid) or []
            entry = self._pick_local(sid, entries)
            if entry is None:
                raise UsageError(
                    f"本机没有找到可批准的 skill：{sid!r}",
                    hint="先运行 `skills-sync plan` 确认 id；同名多副本需先手动处理。",
                )
            if exclude:
                self.state.set_pref(sid, local_only=True, updated_at=util.now_iso())
                self.state.clear_pending(sid)
                self._approvals().pop(sid, None)
                self.state.save()
                results.append({"skill_id": sid, "action": "local-only"})
                continue
            approval = {
                "hash": entry["hash"],
                "name": entry["dir_name"],
                "root": entry["root"],
                "approved_at": util.now_iso(),
                "device": self.cfg.device_name,
            }
            self._approvals()[sid] = approval
            self.state.clear_pending(sid)
            self.state.save()
            results.append({
                "skill_id": sid,
                "action": "approved",
                "hash": entry["hash"],
                "root": entry["root"],
                "excluded": {
                    "secret_files": [x["path"] for x in entry["secret_files"]][:50],
                    "excluded_dirs": [x["path"] for x in entry["excluded_dirs"]][:50],
                },
            })
        return {
            "ok": True,
            "status": STATUS_OK,
            "approved": [r["skill_id"] for r in results if r["action"] == "approved"],
            "local_only": [r["skill_id"] for r in results if r["action"] == "local-only"],
            "results": results,
            "next": "运行 `skills-sync sync` 推送已批准的 skill。",
        }

    # ------------------------------------------------------------------ 卸载 / 恢复 / 全局删除

    def uninstall(self, ids: list[str], *, yes: bool = False, dry_run: bool = False) -> dict:
        for sid in ids:
            _validate_skill_id(sid)
        if not yes and not dry_run:
            raise UsageError(
                "卸载会删除本机 skill 目录，需要加 --yes 确认（会先备份，可用 restore 恢复）。"
            )
        local_scan = scan.scan_roots(self.cfg)
        by_id: dict[str, list[dict]] = {}
        for entry in local_scan["skills"].values():
            by_id.setdefault(entry["id"], []).append(entry)
        planned = []
        for sid in ids:
            entry = self._pick_local(sid, by_id.get(sid, []))
            if entry is None:
                raise UsageError(f"本机没有安装该 skill：{sid!r}")
            planned.append({"skill_id": sid, "path": entry["path"], "root": entry["root"], "hash": entry["hash"]})
        if dry_run:
            return {"status": STATUS_OK, "dry_run": True, "would_uninstall": planned}
        self.state.lock()
        op = self.state.new_op("uninstall", {"ids": ids})
        try:
            for entry in planned:
                sid = entry["skill_id"]
                full_before = util.snapshot_dir(Path(entry["path"]))
                files = util.snapshot_dir(Path(entry["path"]), exclude=self._excluder())
                self.state.backup_skill(
                    op["op_id"], sid, root=entry["root"], install_path=Path(entry["path"]),
                    existed=True, files=files, hash_before=util.tree_hash(files),
                )
                self._remove_dir_versioned(Path(entry["path"]),
                                           {"exists": True, "files": full_before})
                self.state.data["install_map"].pop(sid, None)
                self.state.set_pref(
                    sid, uninstalled=True, uninstalled_at=util.now_iso(), reason="user-uninstall",
                    local_only=False,
                )
                op["skills"][sid] = {"status": "uninstalled", "path": entry["path"]}
            self.state.save()
            self.state.finish_op(op, STATUS_OK, {"uninstalled": ids})
            self.state.save()
            return {
                "status": STATUS_OK,
                "op_id": op["op_id"],
                "uninstalled": ids,
                "note": "只影响本机；共享库与其他设备不受影响。恢复：skills-sync restore <id>。",
            }
        finally:
            self.state.unlock()

    def restore(self, ids: list[str], *, dry_run: bool = False, force: bool = False) -> dict:
        for sid in ids:
            _validate_skill_id(sid)
        self.state.lock()
        try:
            self._prepare_hub(fetch=True)
            hub_scan = scan.scan_hub(self.hub)
            man = self._load_manifest()
            payload = []
            for sid in ids:
                if manifest.is_deleted(man, sid):
                    raise StateError(f"skill {sid!r} 已被全局删除，不能在本机恢复。")
                remote = hub_scan.get(sid)
                if not remote:
                    raise StateError(f"共享库中没有 {sid!r}，无法恢复。")
                root_key, target = self._target_for(sid, (man["skills"].get(sid) or {}).get("name") or sid)
                if not root_key or not target:
                    raise UsageError("找不到可用的安装根目录。")
                payload.append({"skill_id": sid, "root": root_key, "path": str(target)})
            if dry_run:
                return {"status": STATUS_OK, "dry_run": True, "would_restore": payload}

            op = self.state.new_op("restore", {"ids": ids})
            try:
                restored = []
                for plan in payload:
                    sid = plan["skill_id"]
                    src = self.hub / "skills" / sid
                    files, digest = util.snapshot_hash_dir(src)
                    target = Path(plan["path"])
                    conflict = self.state.get_conflict(sid)
                    if conflict:
                        raise StateError(f"skill {sid!r} 存在未解决冲突，请先 resolve/abort。")
                    if target.exists() and not force:
                        current = util.snapshot_dir(target, exclude=self._excluder())
                        current_hash = util.tree_hash(current) if current else ""
                        if current_hash and current_hash != digest:
                            raise StateError(
                                f"本机 {sid!r} 的内容与共享库不同（可能是尚未同步的新改动），"
                                "已停止恢复以免覆盖。",
                                hint="确认要丢弃本机改动时加 --force；或先运行 sync 合并/推送。",
                                details={"skill_id": sid, "path": str(target)},
                            )
                    # 恢复同样是"覆盖本机目录"：绑定此刻看到的本机版本，
                    # 准备安装期间的新改动会被保住而不是被覆盖。
                    current = util.snapshot_dir(target, exclude=self._excluder()) \
                        if target.exists() else {}
                    expect = {
                        "action": "restore",
                        "path": str(target),
                        "exists": target.exists(),
                        "hash": util.tree_hash(current) if current else "",
                    }
                    self._install_content(sid, files, src, plan["root"], target, op,
                                          expect=expect)
                    self.state.set_pref(sid, uninstalled=False, uninstalled_at=None, local_only=False)
                    self.state.save()
                    restored.append(sid)
                self.state.finish_op(op, STATUS_OK, {"restored": restored})
                self.state.save()
                return {"status": STATUS_OK, "op_id": op["op_id"], "restored": restored,
                        "paths": {p["skill_id"]: p["path"] for p in payload}}
            except SyncError as exc:
                op["error"] = exc.message
                self.state.finish_op(op, STATUS_FAILED, {"error": exc.message})
                self.state.save()
                raise
        finally:
            self.state.unlock()

    def delete_shared(self, ids: list[str], *, yes: bool = False) -> dict:
        for sid in ids:
            _validate_skill_id(sid)
        if not yes:
            raise UsageError(
                "全局删除会从共享库删除 skill 并传播到其他设备，需要加 --yes 确认。"
                "只想影响本机请用 `skills-sync uninstall`。"
            )
        self.state.lock()
        op = self.state.new_op("delete-shared", {"ids": ids})
        try:
            self._prepare_hub(fetch=True, op=op)
            hub_scan = scan.scan_hub(self.hub)
            man = self._load_manifest()
            planned = []
            for sid in ids:
                remote = hub_scan.get(sid)
                if not remote or manifest.is_deleted(man, sid):
                    raise UsageError(f"共享库中没有可删除的 skill：{sid!r}")
                planned.append({"skill_id": sid, "hash": remote["hash"], "path": remote["path"]})
            # 本机修改检测：删除会丢内容时必须先解决删除/修改冲突
            local_scan = scan.scan_roots(self.cfg)
            by_id: dict[str, list[dict]] = {}
            for entry in local_scan["skills"].values():
                by_id.setdefault(entry["id"], []).append(entry)
            for plan in planned:
                sid = plan["skill_id"]
                entry = self._pick_local(sid, by_id.get(sid, []))
                base = self.state.baseline(sid)
                if entry and base and entry["hash"] != base["hash"]:
                    self._raise_delete_conflict(plan, entry, base, op)

            commit = None
            for attempt in (1, 2, 3):
                # 每次重试都从当前工作树重新计算，避免把已变化的远端内容当作旧版本删除
                hub_scan = scan.scan_hub(self.hub)
                for plan in planned:
                    fresh = hub_scan.get(plan["skill_id"])
                    if not fresh or fresh["hash"] != plan["hash"]:
                        raise ConflictPending(
                            f"skill {plan['skill_id']!r} 在删除批准后内容发生变化，已停止删除。",
                            hint="重新检查后再执行 delete-shared；旧批准不会覆盖新改动。",
                        )
                man = self._load_manifest()
                for plan in planned:
                    if manifest.get(man, plan["skill_id"]):
                        manifest.tombstone(man, plan["skill_id"], content_hash=plan["hash"])
                    util.remove_dir(self.hub / "skills" / plan["skill_id"])
                manifest.save(self.hub, man)
                commit = self.git.commit_all(self.hub, f"skills-sync: delete shared {', '.join(ids)}")
                if not commit or not self.cfg.remote_url:
                    break
                try:
                    self.git.push(self.hub)
                    break
                except GitError as exc:
                    if not self._push_retryable(exc) or attempt == 3:
                        raise
                    self.git.fetch(self.hub)
                    self._reset_worktree(op)
                    continue

            for plan in planned:
                sid = plan["skill_id"]
                entry = self._pick_local(sid, by_id.get(sid, []))
                if entry is not None:
                    context = {"local_dir": Path(entry["path"]), "local": entry}
                    self._delete_local({"id": sid}, context, op, reason="global-delete")
                else:
                    self.state.clear_baseline(sid)
                    self.state.data["install_map"].pop(sid, None)
                    self.state.set_pref(sid, deleted_shared=True, uninstalled=True)
                self.state.save()
                op["skills"][sid] = {"status": "deleted-shared", "commit": commit}
            self.state.finish_op(op, STATUS_OK, {"deleted": ids})
            self.state.save()
            return {"status": STATUS_OK, "op_id": op["op_id"], "deleted": ids, "commit": commit,
                    "note": "删除已推送到共享库；其他设备下次同步时移除未被修改的副本。"}
        except SyncError as exc:
            op["error"] = str(exc)
            self.state.finish_op(op, STATUS_FAILED, {"error": str(exc)})
            self.state.save()
            raise
        finally:
            self.state.unlock()

    def _raise_delete_conflict(self, plan: dict, entry: dict, base: dict, op: dict) -> None:
        sid = plan["skill_id"]
        tmp = self._tmp_dir(op, f"delconf-{sid}")
        util.copy_selected(Path(entry["path"]), entry["files"], tmp)
        files = util.snapshot_dir(tmp)
        conflict = self.state.add_conflict(
            sid,
            kind="delete-modify",
            local_dir=tmp,
            remote_dir=None,
            base_dir=self.state.base_content_dir(sid) if self.state.base_content_dir(sid).is_dir() else None,
            local_files=files,
            remote_files={},
            base_files=self.state.baseline_files(sid),
            local_hash=entry["hash"],
            remote_hash=plan["hash"],
            base_hash=base["hash"],
            options=["local", "remote"],
            detail="本机内容在基线之后被修改，同时收到全局删除请求；请选择保留本机版本或接受删除。",
            diff_text="本机内容与基线不同，全局删除会丢失这些改动。\n",
            extra={
                "candidates": {"local": str(tmp), "remote": ""},
                "requested": "delete-shared",
                "input": {
                    "local": {"exists": True, "hash": entry["hash"]},
                    "remote": {"exists": True, "hash": plan["hash"], "tomb": False},
                    "base": {"exists": True, "hash": base["hash"]},
                    "candidates": {},
                },
            },
        )
        self.state.save()
        raise ConflictPending(
            f"skill {sid!r} 本机有未同步修改，拒绝直接全局删除。",
            hint=f"先运行 `skills-sync resolve {sid} --take local|remote` 再 `skills-sync continue`。",
            details={"conflict_id": conflict["conflict_id"]},
        )

    # ------------------------------------------------------------------ 冲突解决

    def resolve(self, ids: list[str], *, take: str = "", directory: str = "") -> dict:
        known = {c.get("conflict_id") for c in self.state.conflicts()}
        for sid in ids:
            if sid not in known:  # 冲突编号不是 skill id，不做单段名校验
                _validate_skill_id(sid)
        if not take and not directory:
            raise UsageError("需要 --take local|remote|<root> 或 --dir <合并结果目录>。")
        self.state.lock()
        try:
            return self._resolve_locked(ids, take, directory)
        finally:
            self.state.unlock()

    def _resolve_locked(self, ids: list[str], take: str, directory: str) -> dict:
        resolved = []
        for sid in ids:
            entry = self.state.get_conflict(sid)
            if not entry:
                # 允许直接用冲突编号（conflict_id）操作
                for cand in self.state.conflicts():
                    if cand.get("conflict_id") == sid:
                        sid = cand["skill_id"]
                        entry = cand
                        break
            if not entry:
                raise UsageError(f"skill {sid!r} 没有待处理冲突。")
            target_dir = self._conflict_dir(entry)
            candidates = entry.get("candidates") or {}
            intent = self._conflict_intent(entry)
            if directory:
                source = util.expand_path(directory)
                if not source.is_dir():
                    raise UsageError(f"合并结果目录不存在：{source}")
                mode = "content"
                take_value = "dir"
            elif take == "remote" and intent == "global-delete":
                # 意图(global-delete)与候选内容(candidates)分开建模：
                # "接受远端"对全局删除请求始终等于"批准执行这次删除"，与 hub 此刻是否
                # 还有内容无关（刷新出的新冲突版本会把 hub 内容再当证据填回候选）。
                source = None
                mode = "approve_delete"
                take_value = "remote"
            elif take == "remote" and intent == "remote-delete":
                # 远端确实已经删掉了：take=remote 是"接受已发生的删除"，不写墓碑
                source = None
                mode = "delete"
                take_value = "remote"
            else:
                if take not in entry["options"] and take not in ("local", "remote"):
                    raise UsageError(
                        f"无效选择 {take!r}；可选：{', '.join(entry['options'])}"
                        + ("，或用 --dir 指定合并结果目录。" if entry["kind"] == "content" else "")
                    )
                source = self._candidate_path(entry, take)
                if source is None or not source.is_dir():
                    raise UsageError(f"冲突候选中没有 {take!r} 的内容。")
                mode = "content"
                take_value = take

            out = target_dir / "resolved"
            if out.exists():
                util.remove_dir(out)
            if mode in ("delete", "approve_delete"):
                files, digest = {}, ""
            else:
                if directory:
                    files, digest = self._snapshot_user_dir(source)
                else:
                    files = util.snapshot_dir(source)
                    digest = util.tree_hash(files)
                if not files:
                    raise UsageError("解决方案内容为空，拒绝应用；如果确实要删除请用 --take remote。")
                util.copy_selected(source, files, out)
            self.state.resolve_conflict(sid, {
                "take": take_value,
                "mode": mode,
                "intent": intent,
                "hash": digest,
                "files": files,
                "dir": str(out) if mode == "content" else "",
                # 同名多 root 时 take 就是 root key：把"选中的来源 root"一起记下来，
                # continue 才会把内容装回用户选的那个根，而不是默认根。
                "root": take_value if self.cfg.root_by_key(take_value) else "",
                "at": util.now_iso(),
            })
            self.state.save()
            resolved.append({
                "skill_id": sid,
                "take": take_value,
                "mode": mode,
                "intent": intent,
                "hash": digest,
                "files": len(files),
                "conflict_id": entry["conflict_id"],
                "next": "运行 `skills-sync continue` 应用解决方案。",
            })
        return {"status": STATUS_OK, "resolved": resolved}

    def _conflict_dir(self, entry: dict) -> Path:
        return self.state.conflict_dir(entry["conflict_id"])

    def _candidate_path(self, entry: dict, take: str) -> Path | None:
        candidates = entry.get("candidates") or {}
        paths = entry.get("paths") or {}
        if take in candidates:
            raw = candidates[take]
            return Path(raw) if raw else None
        raw = paths.get(take)
        return Path(raw) if raw else None

    def abort(self, ids: list[str] | None = None) -> dict:
        self.state.lock()
        try:
            targets = ids or [c["skill_id"] for c in self.state.conflicts()]
            cleared = []
            for sid in targets:
                if self.state.get_conflict(sid):
                    self.state.clear_conflict(sid)
                    cleared.append(sid)
            self.state.save()
            return {"status": STATUS_OK, "aborted": cleared,
                    "note": "冲突候选与解决结果已删除；本机与共享库内容未被修改。"}
        finally:
            self.state.unlock()

    def continue_(self, ids: list[str] | None = None) -> dict:
        entries = self.state.conflicts()
        if ids:
            missing = [sid for sid in ids if not self.state.get_conflict(sid)]
            if missing:
                raise UsageError(f"没有这些 skill 的冲突：{', '.join(missing)}")
            entries = [self.state.get_conflict(sid) for sid in ids]
        if not entries:
            return {"status": STATUS_OK, "message": "没有待处理的冲突。", "applied": []}
        unresolved = [e["skill_id"] for e in entries if not e.get("resolution")]
        if unresolved:
            raise ConflictPending(
                f"仍有 {len(unresolved)} 个冲突未解决：{', '.join(sorted(unresolved))}",
                hint="先运行 `skills-sync resolve <id> --take ...` 记录解决方案。",
                details={"unresolved": unresolved},
            )

        self.state.lock()
        op = self.state.new_op("continue", {"ids": [e["skill_id"] for e in entries]})
        try:
            return self._continue(op, entries)
        except SyncError as exc:
            status = STATUS_PARTIAL if _op_has_progress(op) else STATUS_FAILED
            self.state.finish_op(op, status, {"error": exc.message, "hint": exc.hint})
            self.state.save()
            raise
        finally:
            self.state.unlock()

    def _continue(self, op: dict, entries: list[dict]) -> dict:
        self._prepare_hub(fetch=True, op=op)
        hub = self.hub
        written: dict[str, dict] = {}
        local_after: dict[str, dict] = {}
        delete_mode: dict[str, bool] = {}
        approve_delete: dict[str, bool] = {}
        pushed: list[str] = []

        for attempt in range(1, 4):
            written, local_after, delete_mode, approve_delete = self._apply_resolutions(entries, op)
            commit = self.git.commit_all(hub, f"skills-sync: resolve conflicts [{op['op_id']}]")
            if not commit or not self.cfg.remote_url:
                break
            self._fault("push")
            try:
                self.git.push(hub)
            except GitError as exc:
                if not self._push_retryable(exc) or attempt == 3:
                    raise
                self.git.fetch(hub)
                self._reset_worktree(op)
                continue
            pushed = sorted(written)
            break
        else:
            pushed = []

        applied = []
        for entry in entries:
            sid = entry["skill_id"]
            resolution = entry["resolution"]
            if sid in delete_mode or sid in approve_delete:
                # 删除只在"接受已发生的远端删除"或"批准并成功推送全局删除"后执行
                if sid in approve_delete and self.cfg.remote_url and sid not in pushed:
                    raise ConflictPending(
                        f"skill {sid!r} 的全局删除尚未成功推送到共享库，已停止删除本机副本。",
                        hint="检查网络/权限后重新运行 `skills-sync continue`；本机内容仍在。",
                        details={"skill_id": sid, "phase": "push-before-delete"},
                    )
                recorded = (self._entry_input(entry).get("local") or {})
                local = self._local_path(sid)
                local_entry = self._local_entry(sid)
                if local is None and local_entry:
                    local = Path(local_entry["path"])
                root_key = (local_entry or {}).get("root") or (self.state.install(sid) or {}).get("root") \
                    or self.state.pref(sid).get("preferred_root") or self.cfg.default_root
                context = {"local_dir": local,
                           "local": {"root": root_key, "path": str(local) if local else ""}}
                expect = {
                    "action": "delete",
                    "path": str(local) if local else "",
                    "exists": bool(recorded.get("exists", True)),
                    "hash": recorded.get("hash", ""),
                }
                self._delete_local(
                    {"id": sid, "expect": expect}, context, op,
                    reason="global-delete" if sid in approve_delete else "remote-delete",
                )
            elif sid in written:
                install = self.state.install(sid) or {}
                root_key = resolution.get("root") or install.get("root") \
                    or self.state.pref(sid).get("preferred_root") or self.cfg.default_root
                target = resolution.get("target") or ""
                if target:
                    target_path = Path(target)
                else:
                    root = self.cfg.root_by_key(root_key)
                    target_path = (Path(root["path"]) / safe_dir_name(
                        (self._load_manifest().get("skills", {}).get(sid, {}) or {}).get("name") or sid, sid)
                        if root else None)
                if target_path is None:
                    raise StateError(f"找不到 {sid!r} 的安装目标。")
                # 这次批准所依据的本机版本：内容解决/部署必须绑定它，推送完成到落地
                # 之间出现的新改动要保住（不覆盖，也不写"已部署"的假基线）
                expect = self._resolution_expect(entry, target_path)
                local = self._local_path(sid)
                if local and local.is_dir() and util.tree_equals(
                    util.snapshot_dir(local, exclude=self._excluder()), resolution["files"]
                ):
                    self._baseline_from_hub(sid, {"id": sid, "root": root_key, "path": str(local),
                                                  "name": sid}, op)
                else:
                    src = hub / "skills" / sid
                    files, _ = util.snapshot_hash_dir(src)
                    try:
                        self._install_content(sid, files, src, root_key, target_path, op,
                                              expect=expect, pushed=pushed)
                    except ConflictPending as exc:
                        raise self._undeployed_after_push(sid, pushed, exc) from exc
                self.state.set_pref(sid, uninstalled=False, uninstalled_at=None,
                                    preferred_root=root_key)
            self.state.clear_conflict(sid)
            op["skills"][sid] = {"status": "resolved", "at": util.now_iso()}
            self.state.save_op(op)
            applied.append(sid)
            self.state.save()

        self.state.finish_op(op, STATUS_OK, {"applied": applied, "pushed": pushed})
        self.state.save()
        return {"status": STATUS_OK, "op_id": op["op_id"], "applied": applied, "pushed": pushed}

    def _local_path(self, sid: str) -> Path | None:
        install = self.state.install(sid)
        if install and install.get("path"):
            return Path(install["path"])
        # 还未通过本工具安装、但本机已存在的同名 skill（例如首次汇总时的本地副本）
        entry = self._local_entry(sid)
        return Path(entry["path"]) if entry else None

    def _local_entry(self, sid: str) -> dict | None:
        local_scan = scan.scan_roots(self.cfg)
        entries = [e for e in local_scan["skills"].values() if e["id"] == sid]
        return self._pick_local(sid, entries)

    def _resolution_expect(self, entry: dict, target: Path) -> dict:
        """把"批准时的本机版本"变成替换前的复核条件。

        * 同名双根冲突：批准选中的那个 root 的候选哈希（未选中的 root 不在本次范围内）；
        * 其余冲突：冲突记录里本机内容的存在状态与哈希。

        记录缺失（旧格式冲突）时返回空字典，仍会走"准备副本前后哈希比对"的兜底复核。
        """
        recorded = self._entry_input(entry)
        if entry.get("kind") == "same-name-two-roots":
            chosen = (entry.get("resolution") or {}).get("take") or ""
            cand_hash = (recorded.get("candidates") or {}).get(chosen) or ""
            if not cand_hash:
                return {}
            return {"action": "resolve", "path": str(target), "exists": True, "hash": cand_hash}
        local = recorded.get("local") or {}
        if not local:
            return {}
        return {"action": "resolve", "path": str(target),
                "exists": bool(local.get("exists")), "hash": local.get("hash") or ""}

    def _undeployed_after_push(self, sid: str, pushed: list[str],
                               exc: ConflictPending) -> ConflictPending:
        """远端已推送、本机落地前发现新改动：保住本机并明确报告"未部署"。

        不写基线（避免把未落地的内容记成已同步），也不回滚远端——远端提交本身是
        批准过的、且可能已被其他设备拉到。
        """
        if not pushed:
            return exc
        return ConflictPending(
            f"共享库已提交并推送成功，但本机 {sid!r} 在落地前又发生变化，本机未部署。",
            hint="本机内容已完整保留；确认改动后重新运行 `skills-sync sync`，"
                 "工具会按最新内容重新计算，不会重复推送已提交的内容。",
            details={"skill_id": sid, "phase": "pushed-not-deployed",
                     "pushed": list(pushed),
                     "path": (exc.details or {}).get("path", ""),
                     "reason": exc.message},
        )

    def _apply_resolutions(self, entries: list[dict], op: dict) -> tuple[dict, dict, dict, dict]:
        hub = self.hub
        man = self._load_manifest()
        written: dict[str, dict] = {}
        local_after: dict[str, dict] = {}
        delete_mode: dict[str, bool] = {}
        approve_delete: dict[str, bool] = {}
        for entry in entries:
            sid = entry["skill_id"]
            resolution = entry["resolution"]
            kind = entry["kind"]
            resolved_dir = Path(resolution.get("dir") or "")
            mode = resolution.get("mode")
            if mode in ("delete", "approve_delete"):
                # 删除类解决：先按冲突记录的输入版本复核，再决定后续动作
                self._verify_conflict_still_valid(entry, op)
                if mode == "delete":
                    delete_mode[sid] = True
                else:
                    approve_delete[sid] = True
                    # 批准全局删除：写墓碑并移除共享库内容，随本次提交/推送一起生效
                    dest = hub / "skills" / sid
                    recorded_remote = (self._entry_input(entry).get("remote") or {})
                    if dest.exists():
                        util.remove_dir(dest)
                    manifest.tombstone(man, sid, content_hash=recorded_remote.get("hash")
                                       or entry.get("remote_hash") or "")
                    written[sid] = {"files": {}, "hash": "", "path": dest, "deleted": True}
                continue
            if not resolved_dir.is_dir():
                raise StateError(f"skill {sid!r} 的解决方案内容缺失，请重新 resolve。")
            files = util.snapshot_dir(resolved_dir)
            digest = util.tree_hash(files)
            if digest != resolution.get("hash"):
                raise ConflictPending(
                    f"skill {sid!r} 的解决方案内容在批准后发生变化，已停止执行。",
                    hint="重新运行 resolve 生成新的解决方案。",
                )
            self._verify_conflict_still_valid(entry, op)

            dest = hub / "skills" / sid
            tmp = self._tmp_dir(op, f"res-{sid}")
            util.copy_selected(resolved_dir, files, tmp)
            self._fault("hub-write")
            self._replace_dir(tmp, dest)
            manifest.upsert(man, sid, name=(man.get("skills", {}).get(sid, {}) or {}).get("name") or sid,
                            files=files, content_hash=digest)
            written[sid] = {"files": files, "hash": digest, "path": dest}
            if kind == "same-name-two-roots":
                candidates = entry.get("candidates") or {}
                chosen = resolution.get("take")
                resolution["root"] = chosen
                resolution["target"] = candidates.get(chosen, "")
                local_after[sid] = {"root": chosen, "path": candidates.get(chosen, "")}
            else:
                local = self._local_path(sid)
                install = self.state.install(sid) or {}
                root_key = (install.get("root")
                            or self.state.pref(sid).get("preferred_root")
                            or self.cfg.default_root)
                resolution["root"] = root_key
                if local:
                    resolution["target"] = str(local)
        manifest.save(hub, man)
        for entry in entries:
            self.state.resolve_conflict(entry["skill_id"], entry["resolution"])
        return written, local_after, delete_mode, approve_delete

    def _current_input(self, sid: str) -> tuple[dict, dict | None]:
        """当前(本机/共享库/基线/墓碑/各根候选)的完整输入指纹。"""
        local_scan = scan.scan_roots(self.cfg)
        entries = [e for e in local_scan["skills"].values() if e["id"] == sid]
        same = next((c for c in local_scan["same_name_conflicts"] if c["id"] == sid), None)
        entry = self._pick_local(sid, entries)
        hub_scan = scan.scan_hub(self.hub) if self._hub_ready() else {}
        remote = hub_scan.get(sid)
        base = self.state.baseline(sid)
        man = self._load_manifest()
        tomb = bool((man.get("skills", {}).get(sid) or {}).get("deleted"))
        candidates = {}
        if entry is None and same:
            candidates = {e["root"]: e["hash"] for e in same["entries"]}
        current = {
            "local": {"exists": entry is not None, "hash": entry["hash"] if entry else ""},
            "remote": {"exists": remote is not None, "hash": remote["hash"] if remote else "",
                       "tomb": tomb},
            "base": {"exists": base is not None, "hash": base["hash"] if base else ""},
            "candidates": candidates,
        }
        return current, entry

    def _verify_conflict_still_valid(self, entry: dict, op: dict) -> None:
        """按冲突记录的完整输入版本复核；任何变化都作废旧批准并重建冲突版本。"""
        sid = entry["skill_id"]
        recorded = self._entry_input(entry)
        current, _local = self._current_input(sid)
        changed: list[str] = []
        if recorded:
            labels = {
                "local": "本机内容",
                "remote": "共享库/墓碑状态",
                "base": "基线",
                "candidates": "同名候选内容",
            }
            for key, label in labels.items():
                if (recorded.get(key) or {}) != (current.get(key) or {}):
                    changed.append(label)
        else:
            # 旧记录没有完整指纹：退化为本机/共享库哈希比较
            if entry.get("local_hash") and current["local"]["hash"] != entry["local_hash"]:
                changed.append("本机内容")
            if entry.get("remote_hash") and current["remote"]["hash"] != entry["remote_hash"]:
                changed.append("共享库内容")
        if not changed:
            return
        new_entry = self._new_conflict_version(entry, op)
        raise ConflictPending(
            f"skill {sid!r} 的{'、'.join(changed)}在冲突批准后发生变化，旧批准不再适用。",
            hint="旧快照已保留；已按当前内容生成新冲突（"
            + (new_entry or {}).get("conflict_id", "无")
            + "），请重新 resolve 后再 continue。",
            details={"skill_id": sid, "changed": changed,
                     "conflict_id": (new_entry or {}).get("conflict_id", "")},
        )

    def _new_conflict_version(self, old: dict, op: dict) -> dict | None:
        """按当前内容重建一个冲突版本；旧冲突目录与快照保持不动。"""
        sid = old["skill_id"]
        local_scan = scan.scan_roots(self.cfg)
        entries = [e for e in local_scan["skills"].values() if e["id"] == sid]
        same = next((c for c in local_scan["same_name_conflicts"] if c["id"] == sid), None)
        local_entry = self._pick_local(sid, entries)
        hub_scan = scan.scan_hub(self.hub) if self._hub_ready() else {}
        remote = hub_scan.get(sid)
        base = self.state.baseline(sid)
        base_dir = self.state.base_content_dir(sid)
        current, _ = self._current_input(sid)
        candidates = current.get("candidates") or {}
        item = {
            "id": sid,
            "name": (local_entry or {}).get("dir_name") or sid,
            "kind": old.get("kind") or "content",
            "detail": (old.get("detail") or "内容冲突") + "（按最新内容重新生成的新冲突版本）",
            "options": list(old.get("options") or ["local", "remote"]),
            "input": current,
            "local_hash": current["local"]["hash"],
            "remote_hash": current["remote"]["hash"],
            "base_hash": current["base"]["hash"],
            "supersedes": old.get("conflict_id") or "",
        }
        context = {
            "entries": entries,
            "local": local_entry,
            "local_dir": Path(local_entry["path"]) if local_entry else None,
            "local_files": local_entry["files"] if local_entry else {},
            "remote_dir": Path(remote["path"]) if remote and (remote.get("path")) else None,
            "remote_files": remote["files"] if remote else {},
            "base_dir": base_dir if base and base_dir.is_dir() else None,
            "base_files": self.state.baseline_files(sid) if base else {},
            "same_name": same,
            "existing": None,
            "merge": None,
            "input": current,
        }
        if candidates and same:
            context["same_name"] = same
        return self._ensure_conflict(item, context, op)

    # ------------------------------------------------------------------ 恢复 / 诊断

    def recover(self, *, restore: str = "", force: bool = False, prune_baseline: bool = False) -> dict:
        if restore:
            return self._restore_op(restore, force=force)
        if prune_baseline:
            return self._prune_baselines()
        hub = self.hub
        info = {
            "status": STATUS_OK,
            "hub": str(hub),
            "hub_repo": self.git.is_repo(hub),
            "hub_head": None,
            "remote_url": util.redact_url(self.cfg.remote_url),
            "remote_head": None,
            "dirty": [],
            "conflicts": [],
            "incomplete_ops": [],
            "backups": [],
            "warnings": [],
        }
        if info["hub_repo"]:
            info["hub_head"] = self.git.head(hub)
            if self.cfg.remote_url:
                info["remote_head"] = self.git.remote_head(hub, self.cfg.branch)
            info["dirty"] = self.git.dirty(hub)
            if info["dirty"]:
                info["warnings"].append(
                    "共享库有未提交改动；确认无用后可在 hub 目录执行 "
                    f"`git reset --hard origin/{self.cfg.branch}`。"
                )
        else:
            info["warnings"].append("共享库尚未初始化；运行 `skills-sync sync` 会克隆或初始化。")
        info["conflicts"] = [
            {"skill_id": c["skill_id"], "kind": c["kind"], "created_at": c["created_at"],
             "resolved": bool(c.get("resolution")), "conflict_id": c["conflict_id"]}
            for c in self.state.conflicts()
        ]
        for op in self.state.recent_ops(20):
            if op.get("status") == "running":
                info["incomplete_ops"].append(op["op_id"])
            backups = self.state.list_backups(op["op_id"])
            if backups:
                info["backups"].append({"op_id": op["op_id"], "skills": backups})
        if info["conflicts"]:
            info["status"] = STATUS_NEEDS_ACTION
        return info

    def _restore_op(self, spec: str, *, force: bool) -> dict:
        # 恢复会改本机目录与状态：与 sync/continue/restore 等写操作共用同一把锁，
        # 避免两个进程同时替换同一个目标目录。
        self.state.lock()
        try:
            return self._restore_op_locked(spec, force=force)
        finally:
            self.state.unlock()

    def _restore_op_locked(self, spec: str, *, force: bool) -> dict:
        if "/" not in spec:
            raise UsageError("格式应为 --restore <op_id>/<skill_id>。")
        op_id, sid = spec.split("/", 1)
        meta = self.state.read_backup(op_id, sid)
        if not meta:
            raise UsageError(f"没有找到备份：{spec}")
        target = Path(meta["install_path"])
        root = self.cfg.root_by_key(meta["root"])
        if root:
            util.require_within(target, Path(root["path"]), "恢复目标")
        current = util.snapshot_dir(target) if target.exists() else {}
        current_hash = util.tree_hash(current) if current else ""
        if current_hash and current_hash != meta.get("hash_before") and not force:
            raise StateError(
                "恢复目标在备份之后又发生了变化，为避免覆盖新改动已停止。",
                hint="确认要丢弃这些改动时加 --force；或先手工备份当前内容。",
                details={"target": str(target), "current_hash": current_hash},
            )
        self._restore_backup_checked(op_id, sid, target, spec)
        self.state.set_pref(sid, uninstalled=False, uninstalled_at=None)
        self.state.save()
        return {"status": STATUS_OK, "restored": {"op_id": op_id, "skill_id": sid,
                                                  "path": str(target), "forced": force}}

    def _restore_backup_checked(self, op_id: str, sid: str, target: Path, spec: str) -> None:
        """恢复入口：失败必须如实报错，备份保留可重试，不写偏好。"""
        if self._restore_backup(op_id, sid, target):
            return
        raise StateError(
            f"从备份恢复失败：{spec} 未能完整写回 {target}。",
            hint="备份仍保留在原处可重试；检查目标目录权限、占用与磁盘空间后重新执行同一 recover 命令。",
            details={"op_id": op_id, "skill_id": sid, "target": str(target),
                     "backup_dir": str(self.state.backup_dir(op_id, sid))},
        )

    def _prune_baselines(self) -> dict:
        # 清理基线会改状态：同样串行化，避免与并发的 sync/continue 互相覆盖
        self.state.lock()
        try:
            return self._prune_baselines_locked()
        finally:
            self.state.unlock()

    def _prune_baselines_locked(self) -> dict:
        removed = []
        hub_scan = scan.scan_hub(self.hub) if self._hub_ready() else {}
        man = self._load_manifest() if self._hub_ready() else manifest.empty()
        for sid in list(self.state.data["baselines"]):
            if sid in hub_scan or self.state.install(sid):
                continue
            self.state.clear_baseline(sid)
            removed.append(sid)
        self.state.save()
        return {"status": STATUS_OK, "pruned": removed}

    def doctor(self) -> dict:
        import platform
        import sys

        report = {
            "status": STATUS_OK,
            "python": {
                "version": sys.version.split()[0],
                "executable": sys.executable,
                "platform": platform.platform(),
            },
            "git": {"version": "", "ok": True},
            "config": {"path": str(self.cfg.path), "ok": True},
            "hub": {"path": str(self.hub), "repo": self.git.is_repo(self.hub)},
            "state": {"path": str(self.state.dir)},
            "roots": [],
            "remote": {"url": util.redact_url(self.cfg.remote_url), "ok": None, "detail": ""},
            "warnings": [],
            "checks": [],
        }
        try:
            report["git"]["version"] = self.git.version()
        except GitError as exc:
            report["git"].update(ok=False, error=exc.message)
            report["warnings"].append("未找到可用的 git，同步无法工作。")
        for root in self.cfg.roots:
            path = Path(root["path"])
            entry = {"key": root["key"], "path": str(path), "exists": path.is_dir(),
                     "writable": os.access(str(path), os.W_OK) if path.is_dir() else False}
            if not entry["exists"]:
                report["warnings"].append(f"skill 根不存在：{path}")
            report["roots"].append(entry)
        if sys.version_info < (3, 9):
            report["warnings"].append("Python 版本低于 3.9，未测试。")
        try:
            self.state.dir.mkdir(parents=True, exist_ok=True)
            probe = self.state.dir / ".write-test"
            probe.write_text("ok", "utf-8")
            probe.unlink()
        except OSError as exc:
            report["warnings"].append(f"状态目录不可写：{exc}")
        if self.cfg.remote_url:
            ok, detail = self.git.ls_remote(self.cfg.remote_url)
            report["remote"].update(ok=ok, detail=detail[:500])
            if not ok:
                report["warnings"].append(
                    "远端不可达（离线时仍可 plan；sync 会明确失败且不改动本机）。"
                )
        if self.git.is_repo(self.hub):
            report["hub"]["head"] = self.git.head(self.hub)
            report["hub"]["branch"] = self.git.branch_of(self.hub)
            dirty = self.git.dirty(self.hub)
            report["hub"]["dirty"] = dirty[:20]
            if dirty:
                report["warnings"].append("共享库有未提交改动，sync 会拒绝执行。")
        if report["warnings"]:
            report["status"] = STATUS_NEEDS_ACTION
        return report

    def status(self) -> dict:
        return {
            "status": STATUS_OK,
            "device": self.cfg.device_name,
            "config": str(self.cfg.path),
            "hub": str(self.hub),
            "hub_ready": self._hub_ready(),
            "remote_url": self.cfg.remote_url,
            "branch": self.cfg.branch,
            "conflicts": [
                {"skill_id": c["skill_id"], "kind": c["kind"], "resolved": bool(c.get("resolution")),
                 "created_at": c["created_at"]}
                for c in self.state.conflicts()
            ],
            "pending_additions": sorted(self.state.data["pending_additions"]),
            "uninstalled": sorted(k for k, v in self.state.data["prefs"].items() if v.get("uninstalled")),
            "local_only": sorted(k for k, v in self.state.data["prefs"].items() if v.get("local_only")),
            "recent_ops": [
                {"op_id": op["op_id"], "kind": op["kind"], "status": op["status"],
                 "started_at": op["started_at"], "finished_at": op.get("finished_at")}
                for op in self.state.recent_ops(5)
            ],
        }

    def compat(self) -> dict:
        local_scan = scan.scan_roots(self.cfg)
        skills = []
        counts: dict[str, int] = {}
        for entry in sorted(local_scan["skills"].values(), key=lambda e: e["id"]):
            findings = entry["compat"]
            for finding in findings:
                counts[finding["code"]] = counts.get(finding["code"], 0) + 1
            if findings:
                skills.append({
                    "id": entry["id"],
                    "name": entry["dir_name"],
                    "root": entry["root"],
                    "findings": findings,
                })
        notes = [
            "这些只是跨平台兼容提示：文件已完整同步，但不代表在另一平台可直接运行。",
            "工具不会自动修改脚本或路径。",
        ]
        if all(not s["findings"] for s in skills):
            notes.append("未发现明显的平台相关写法；仍建议在目标平台实跑一次。")
        return {"status": STATUS_OK, "skills": skills, "counts": counts, "notes": notes}


def _summarize(items: list[dict]) -> dict:
    summary: dict[str, int] = {}
    for item in items:
        summary[item["action"]] = summary.get(item["action"], 0) + 1
    summary["total"] = len(items)
    summary["needs_action"] = sum(1 for i in items if i["needs_action"])
    summary["warnings"] = sum(len(i.get("warnings") or []) for i in items)
    return summary


def _op_has_progress(op: dict) -> bool:
    for entry in (op.get("skills") or {}).values():
        if entry.get("status") in ("pushed", "deployed", "resolved", "uninstalled", "deleted-shared"):
            return True
    return False
