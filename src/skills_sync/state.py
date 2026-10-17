"""本机状态：部署基线、安装映射、本机偏好、待审批项、冲突候选、操作记录、备份。

这些数据全部保存在共享仓库之外，不随同步传播（R-04/R-07 的本机语义）。
"""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from pathlib import Path

from . import util
from .errors import StateError

STATE_SCHEMA = 1
LOCK_STALE_SECONDS = 30 * 60


class State:
    def __init__(self, cfg):
        self.cfg = cfg
        self.dir = Path(cfg.state_dir)
        self.path = self.dir / "state.json"
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "skills").mkdir(exist_ok=True)
        (self.dir / "conflicts").mkdir(exist_ok=True)
        (self.dir / "pending").mkdir(exist_ok=True)
        (self.dir / "ops").mkdir(exist_ok=True)
        (self.dir / "backups").mkdir(exist_ok=True)
        (self.dir / "logs").mkdir(exist_ok=True)
        data = None
        if self.path.exists():
            try:
                data = util.read_json(self.path)
            except ValueError as exc:
                raise StateError(str(exc), hint="状态文件损坏；请从备份或另一台设备恢复，不要手工删改。")
        if not data:
            data = {
                "schema": STATE_SCHEMA,
                "device_id": uuid.uuid4().hex,
                "device_name": cfg.device_name,
                "created_at": util.now_iso(),
                "baselines": {},
                "install_map": {},
                "prefs": {},
                "pending_additions": {},
                "conflicts": {},
                "ops": {},
            }
        if data.get("schema") != STATE_SCHEMA:
            raise StateError(f"状态 schema 不支持：{data.get('schema')!r}")
        self.data = data
        self._lock_path = self.dir / "lock"

    # ---------- 持久化 ----------

    def save(self) -> None:
        util.write_json(self.path, self.data)

    # ---------- 锁 ----------

    def lock(self) -> None:
        deadline = time.time()
        while True:
            try:
                fd = os.open(str(self._lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(json.dumps({"pid": os.getpid(), "at": util.now_iso()}))
                return
            except FileExistsError:
                age = time.time() - self._lock_path.stat().st_mtime
                if age > LOCK_STALE_SECONDS:
                    try:
                        self._lock_path.unlink()
                    except OSError:
                        pass
                    continue
                if time.time() > deadline + 1:
                    raise StateError(
                        f"另一个同步进程正在运行（锁文件：{self._lock_path}）",
                        hint=f"若确认没有其他进程，删除该锁文件后重试。",
                    )
                time.sleep(0.2)

    def unlock(self) -> None:
        try:
            self._lock_path.unlink()
        except OSError:
            pass

    # ---------- 基线 ----------

    def skill_dir(self, skill_id: str) -> Path:
        return self.dir / "skills" / skill_id

    def baseline(self, skill_id: str) -> dict | None:
        entry = self.data["baselines"].get(skill_id)
        if not entry:
            return None
        return entry

    def baseline_files(self, skill_id: str) -> dict[str, dict]:
        path = self.skill_dir(skill_id) / "baseline.json"
        if not path.exists():
            return {}
        try:
            return (util.read_json(path) or {}).get("files", {})
        except ValueError:
            return {}

    def base_content_dir(self, skill_id: str) -> Path:
        return self.skill_dir(skill_id) / "base"

    def set_baseline(self, skill_id: str, *, files: dict, content_dir: Path, commit: str,
                     root: str, install_path: str) -> None:
        """写入基线内容副本与元数据；content_dir 必须是已就绪的目录。"""
        sdir = self.skill_dir(skill_id)
        sdir.mkdir(parents=True, exist_ok=True)
        base_dir = sdir / "base"
        if base_dir.exists():
            util.remove_dir(base_dir)
        util.copy_tree(content_dir, base_dir)
        digest = util.tree_hash(files)
        util.write_json(
            sdir / "baseline.json",
            {
                "hash": digest,
                "files": files,
                "commit": commit,
                "root": root,
                "install_path": install_path,
                "synced_at": util.now_iso(),
            },
        )
        self.data["baselines"][skill_id] = {
            "hash": digest,
            "commit": commit,
            "root": root,
            "install_path": install_path,
            "synced_at": util.now_iso(),
            "files_count": len(files),
        }

    def clear_baseline(self, skill_id: str) -> None:
        self.data["baselines"].pop(skill_id, None)
        sdir = self.skill_dir(skill_id)
        if sdir.exists():
            util.remove_dir(sdir)

    # ---------- 安装映射 / 偏好 ----------

    def set_install(self, skill_id: str, root: str, path: str) -> None:
        self.data["install_map"][skill_id] = {"root": root, "path": path}

    def install(self, skill_id: str) -> dict | None:
        return self.data["install_map"].get(skill_id)

    def pref(self, skill_id: str) -> dict:
        return self.data["prefs"].get(skill_id, {})

    def set_pref(self, skill_id: str, **kwargs) -> None:
        entry = self.data["prefs"].setdefault(skill_id, {})
        entry.update(kwargs)
        if any(k in kwargs for k in ("local_only", "uninstalled")):
            entry["updated_at"] = util.now_iso()

    def is_local_only(self, skill_id: str) -> bool:
        return bool(self.pref(skill_id).get("local_only"))

    def is_uninstalled(self, skill_id: str) -> bool:
        return bool(self.pref(skill_id).get("uninstalled"))

    # ---------- 待审批新增 ----------

    def pending_dir(self, skill_id: str) -> Path:
        return self.dir / "pending" / skill_id

    def add_pending_addition(self, skill_id: str, *, name: str, files: dict, src_dir: Path,
                             roots: list[str], source: dict, flags: list[dict]) -> dict:
        pdir = self.pending_dir(skill_id)
        if pdir.exists():
            util.remove_dir(pdir)
        pdir.mkdir(parents=True)
        util.copy_selected(src_dir, files, pdir / "files")
        digest = util.tree_hash(files)
        entry = {
            "skill_id": skill_id,
            "name": name,
            "hash": digest,
            "files": files,
            "roots": roots,
            "source": source,
            "flags": flags,
            "first_seen": util.now_iso(),
        }
        util.write_json(pdir / "pending.json", entry)
        self.data["pending_additions"][skill_id] = {
            "name": name,
            "hash": digest,
            "roots": roots,
            "first_seen": entry["first_seen"],
        }
        return entry

    def pending_addition(self, skill_id: str) -> dict | None:
        pdir = self.pending_dir(skill_id)
        meta = pdir / "pending.json"
        if not meta.exists():
            return None
        try:
            return util.read_json(meta)
        except ValueError:
            return None

    def clear_pending(self, skill_id: str) -> None:
        self.data["pending_additions"].pop(skill_id, None)
        pdir = self.pending_dir(skill_id)
        if pdir.exists():
            util.remove_dir(pdir)

    # ---------- 冲突 ----------

    def conflict_dir(self, conflict_id: str) -> Path:
        return self.dir / "conflicts" / conflict_id

    def add_conflict(self, skill_id: str, *, kind: str, local_dir: Path | None, remote_dir: Path | None,
                     base_dir: Path | None, local_files: dict, remote_files: dict, base_files: dict,
                     local_hash: str, remote_hash: str, base_hash: str, options: list[str],
                     detail: str, diff_text: str = "", extra: dict | None = None) -> dict:
        cid = "c-" + util.now_iso().replace(":", "").replace("-", "").replace("+", "")[:15] + "-" + uuid.uuid4().hex[:6]
        cdir = self.conflict_dir(cid)
        cdir.mkdir(parents=True, exist_ok=False)
        paths = {}
        for label, src in (("local", local_dir), ("remote", remote_dir), ("base", base_dir)):
            if src is not None:
                util.copy_tree(src, cdir / label)
                paths[label] = str(cdir / label)
        if diff_text:
            util.atomic_write_text(cdir / "diff.txt", diff_text)
            paths["diff"] = str(cdir / "diff.txt")
        entry = {
            "conflict_id": cid,
            "skill_id": skill_id,
            "kind": kind,
            "created_at": util.now_iso(),
            "detail": detail,
            "options": options,
            "local_hash": local_hash,
            "remote_hash": remote_hash,
            "base_hash": base_hash,
            "local_files": local_files,
            "remote_files": remote_files,
            "base_files": base_files,
            "paths": paths,
            "resolution": None,
        }
        if extra:
            entry.update(extra)
        util.write_json(cdir / "conflict.json", entry)
        self.data["conflicts"][skill_id] = {
            "conflict_id": cid,
            "kind": kind,
            "created_at": entry["created_at"],
            "local_hash": local_hash,
            "remote_hash": remote_hash,
            "base_hash": base_hash,
        }
        return entry

    def get_conflict(self, skill_id: str) -> dict | None:
        index = self.data["conflicts"].get(skill_id)
        if not index:
            return None
        path = self.conflict_dir(index["conflict_id"]) / "conflict.json"
        if not path.exists():
            return None
        try:
            return util.read_json(path)
        except ValueError:
            return None

    def conflicts(self) -> list[dict]:
        out = []
        for skill_id in sorted(self.data["conflicts"]):
            entry = self.get_conflict(skill_id)
            if entry:
                out.append(entry)
        return out

    def resolve_conflict(self, skill_id: str, resolution: dict) -> dict:
        entry = self.get_conflict(skill_id)
        if not entry:
            raise StateError(f"skill {skill_id!r} 没有待处理冲突。")
        entry["resolution"] = resolution
        entry["resolved_at"] = util.now_iso()
        util.write_json(self.conflict_dir(entry["conflict_id"]) / "conflict.json", entry)
        return entry

    def clear_conflict(self, skill_id: str) -> None:
        entry = self.get_conflict(skill_id)
        self.data["conflicts"].pop(skill_id, None)
        if entry:
            cdir = self.conflict_dir(entry["conflict_id"])
            if cdir.exists():
                util.remove_dir(cdir)

    # ---------- 操作记录 ----------

    def op_path(self, op_id: str) -> Path:
        return self.dir / "ops" / f"{op_id}.json"

    def new_op(self, kind: str, params: dict | None = None) -> dict:
        op_id = "op-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        op = {
            "op_id": op_id,
            "kind": kind,
            "device": self.data.get("device_name"),
            "started_at": util.now_iso(),
            "finished_at": None,
            "status": "running",
            "phase": "start",
            "params": params or {},
            "steps": [],
            "skills": {},
        }
        util.write_json(self.op_path(op_id), op)
        self.data["ops"][op_id] = {"kind": kind, "status": "running", "started_at": op["started_at"]}
        self._trim_ops()
        return op

    def save_op(self, op: dict) -> None:
        util.write_json(self.op_path(op["op_id"]), op)

    def op_step(self, op: dict, phase: str, status: str = "ok", **extra) -> None:
        op["phase"] = phase
        op["steps"].append({"at": util.now_iso(), "phase": phase, "status": status, **extra})
        self.save_op(op)

    def finish_op(self, op: dict, status: str, summary: dict | None = None) -> None:
        op["status"] = status
        op["finished_at"] = util.now_iso()
        if summary is not None:
            op["summary"] = summary
        self.save_op(op)
        self.data["ops"][op["op_id"]]["status"] = status
        with open(self.dir / "logs" / "operations.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "at": util.now_iso(),
                "op_id": op["op_id"],
                "kind": op["kind"],
                "status": status,
                "summary": summary or {},
            }, ensure_ascii=False) + "\n")

    def op(self, op_id: str) -> dict | None:
        path = self.op_path(op_id)
        if not path.exists():
            return None
        try:
            return util.read_json(path)
        except ValueError:
            return None

    def recent_ops(self, limit: int = 5) -> list[dict]:
        entries = sorted(self.data["ops"].items(), key=lambda kv: kv[1].get("started_at", ""), reverse=True)
        out = []
        for op_id, meta in entries[:limit]:
            record = self.op(op_id)
            if record:
                out.append(record)
        return out

    def _trim_ops(self) -> None:
        entries = sorted(self.data["ops"].items(), key=lambda kv: kv[1].get("started_at", ""), reverse=True)
        for op_id, _ in entries[200:]:
            self.data["ops"].pop(op_id, None)
            path = self.op_path(op_id)
            if path.exists():
                try:
                    path.unlink()
                except OSError:
                    pass

    # ---------- 备份 ----------

    def backup_dir(self, op_id: str, skill_id: str) -> Path:
        return self.dir / "backups" / op_id / skill_id

    def backup_skill(self, op_id: str, skill_id: str, *, root: str, install_path: Path,
                     existed: bool, files: dict, hash_before: str) -> dict:
        bdir = self.backup_dir(op_id, skill_id)
        if bdir.exists():
            util.remove_dir(bdir)
        bdir.mkdir(parents=True)
        if existed and Path(install_path).exists():
            util.copy_tree(install_path, bdir / "files")
        meta = {
            "skill_id": skill_id,
            "root": root,
            "install_path": str(install_path),
            "existed": existed,
            "hash_before": hash_before,
            "files": files,
            "created_at": util.now_iso(),
        }
        util.write_json(bdir / "meta.json", meta)
        return meta

    def read_backup(self, op_id: str, skill_id: str) -> dict | None:
        meta = self.backup_dir(op_id, skill_id) / "meta.json"
        if not meta.exists():
            return None
        try:
            return util.read_json(meta)
        except ValueError:
            return None

    def list_backups(self, op_id: str) -> list[str]:
        root = self.dir / "backups" / op_id
        if not root.exists():
            return []
        return sorted(p.name for p in root.iterdir() if p.is_dir())
