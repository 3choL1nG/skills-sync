"""共享库清单 manifest.json：skill 身份、来源、内容哈希、全局删除墓碑。"""

from __future__ import annotations

import json
from pathlib import Path

from . import util

SCHEMA = 1
MANIFEST_NAME = "manifest.json"
README = """# skills-sync 共享库

此仓库由 `skills-sync` 管理，用于在用户自己的多台电脑之间同步 Codex skills 的完整文件快照。

- `skills/<skill-id>/`：纳入同步的完整 skill 内容（含脚本、模板、资源、许可证）。
- `manifest.json`：共享清单（身份、来源、内容哈希、全局删除记录）。
- 请勿手工编辑；任何改动都会被下一次同步覆盖或报告。

本仓库不包含任何本机状态（安装位置、仅本机清单、不安装清单、备份、日志）。
"""


def manifest_path(hub: Path) -> Path:
    return Path(hub) / MANIFEST_NAME


def empty() -> dict:
    return {"schema": SCHEMA, "updated_at": util.now_iso(), "skills": {}}


def load(hub: Path) -> dict:
    path = manifest_path(hub)
    if not path.exists():
        return empty()
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"共享清单损坏：{path}（{exc}）") from exc
    if data.get("schema") != SCHEMA:
        raise ValueError(f"共享清单 schema 不支持：{data.get('schema')!r}")
    data.setdefault("skills", {})
    return data


def save(hub: Path, data: dict) -> None:
    data["updated_at"] = util.now_iso()
    util.write_json(manifest_path(hub), data)


def get(data: dict, skill_id: str) -> dict | None:
    return data.get("skills", {}).get(skill_id)


def upsert(data: dict, skill_id: str, *, name: str, files: dict, content_hash: str,
           source: dict | None = None, licenses: list[str] | None = None) -> dict:
    entry = data["skills"].setdefault(skill_id, {})
    entry.update({
        "id": skill_id,
        "name": name,
        "content_hash": content_hash,
        "files_count": len(files),
        "source": source or entry.get("source") or {"type": "unknown"},
        "licenses": licenses if licenses is not None else entry.get("licenses", []),
        "deleted": False,
        "deleted_at": None,
        "deleted_content_hash": None,
        "updated_at": util.now_iso(),
    })
    entry.setdefault("first_seen", entry["updated_at"])
    return entry


def tombstone(data: dict, skill_id: str, *, content_hash: str) -> dict:
    entry = data["skills"].setdefault(skill_id, {"id": skill_id, "name": skill_id})
    entry.update({
        "deleted": True,
        "deleted_at": util.now_iso(),
        "deleted_content_hash": content_hash,
        "content_hash": None,
        "files_count": 0,
    })
    return entry


def active_ids(data: dict) -> list[str]:
    return sorted(k for k, v in data["skills"].items() if not v.get("deleted"))


def is_deleted(data: dict, skill_id: str) -> bool:
    entry = data["skills"].get(skill_id)
    return bool(entry and entry.get("deleted"))


def write_readme(hub: Path) -> None:
    path = Path(hub) / "README.md"
    if not path.exists():
        util.atomic_write_text(path, README)
