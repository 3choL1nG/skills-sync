"""命令行入口：skills-sync <command> [options]。

输出两种形态：
- 默认：给人看的分段文本；
- --json：机器可读的 {ok,status,exit_code,command,data|error,hint}。

退出码：0 成功；1 用法/配置错误；2 执行失败（含部分完成）；3 需要人工处理（冲突/待批准）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from . import config as config_mod
from . import util
from .engine import Engine
from .errors import (
    EXIT_FAILED,
    EXIT_NEEDS_ACTION,
    EXIT_OK,
    EXIT_USAGE,
    STATUS_FAILED,
    STATUS_NEEDS_ACTION,
    STATUS_OK,
    SyncError,
    UsageError,
)

PROG = "skills-sync"


# ---------------------------------------------------------------------- 参数


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", metavar="PATH", default=argparse.SUPPRESS,
                        help="配置文件路径（默认 ~/.skills-sync/config.json，也可用 SKILLS_SYNC_CONFIG）")
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="输出机器可读 JSON")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="在多台设备之间双向同步 Codex 用户 skill（GitHub 私有仓库做中转）。",
        epilog="常用流程：init -> plan -> include <id> -> sync；冲突时 resolve 后 continue。",
    )
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    _add_common(parser)
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    def cmd(name: str, help_text: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text, description=help_text, parents=[_common_parent()])
        return p

    p = cmd("init", "初始化配置、共享库与 skill 根目录（首次使用）")
    p.add_argument("--remote", metavar="URL", default="", help="GitHub 私有仓库 URL；省略则为仅本机模式")
    p.add_argument("--root", action="append", default=[], metavar="KEY=PATH",
                   help="用户 skill 根，可重复，例如 codex=~/.codex/skills")
    p.add_argument("--default-root", metavar="KEY", default="", help="默认安装根（新 skill 部署到这里）")
    p.add_argument("--hub", metavar="DIR", default="hub", help="共享库本地目录（相对配置文件目录）")
    p.add_argument("--state", metavar="DIR", default="state", help="本机状态目录（相对配置文件目录）")
    p.add_argument("--branch", default="main", help="共享库分支名")
    p.add_argument("--name", default="", help="本机设备名（默认取主机名）")
    p.add_argument("--force", action="store_true", help="覆盖已存在的配置文件")

    cmd("doctor", "环境与配置预检（只读）")
    cmd("status", "显示本机状态、冲突与最近操作")

    p = cmd("plan", "生成同步计划（默认先检查远端；不写任何 skill 目录、不改冲突证据）")
    p.add_argument("--offline", action="store_true",
                   help="不访问远端，只用本机已缓存的共享库快照生成计划（会标注未验证最新）")

    p = cmd("sync", "按计划同步：推送本机改动、拉取并部署远端改动")
    p.add_argument("--dry-run", action="store_true", help="只显示计划，不执行任何写入")

    p = cmd("include", "批准新发现的 skill（批准后才推送）")
    p.add_argument("ids", nargs="*", metavar="ID", help="skill id；省略时配合 --all-pending")
    p.add_argument("--all-pending", action="store_true", help="批准所有尚未进入共享库的本机 skill")
    p.add_argument("--exclude", action="store_true", help="标记为仅本机（不推送、不传播）")
    p.add_argument("--root", metavar="KEY", default="",
                   help="同名 skill 同时存在于多个 root 时，指定从哪个 root 批准；"
                        "批准会绑定该 root 及其当时的内容，后续 sync 按这一份处理")

    p = cmd("uninstall", "仅在本机卸载 skill（不影响共享库与其他设备）")
    p.add_argument("ids", nargs="+", metavar="ID")
    p.add_argument("--yes", action="store_true", help="确认执行（会先备份，可用 restore 恢复）")
    p.add_argument("--dry-run", action="store_true", help="只列出将卸载的内容")

    p = cmd("restore", "从共享库或本机备份恢复 skill")
    p.add_argument("ids", nargs="+", metavar="ID")
    p.add_argument("--dry-run", action="store_true", help="只列出将恢复的内容")
    p.add_argument("--force", action="store_true",
                   help="本机内容与共享库不同时也覆盖（会丢弃未同步的新改动）")

    p = cmd("delete-shared", "全局删除 skill 并传播到其他设备（需要 --yes）")
    p.add_argument("ids", nargs="+", metavar="ID")
    p.add_argument("--yes", action="store_true", help="确认全局删除")

    p = cmd("resolve", "为冲突记录解决方案")
    p.add_argument("ids", nargs="+", metavar="ID")
    p.add_argument("--take", default="", metavar="local|remote|<root>",
                   help="保留哪一方；对删除冲突用 remote 表示接受删除")
    p.add_argument("--dir", default="", metavar="DIR", help="使用手工合并后的目录内容")

    p = cmd("continue", "应用已记录的冲突解决方案")
    p.add_argument("ids", nargs="*", metavar="ID")

    p = cmd("abort", "放弃冲突（候选与解决结果会被删除，不改动本机与共享库）")
    p.add_argument("ids", nargs="*", metavar="ID")

    p = cmd("recover", "查看未完成操作/备份；从备份恢复；清理过期基线")
    p.add_argument("--restore", default="", metavar="OP_ID/SKILL_ID", help="从指定备份恢复")
    p.add_argument("--force", action="store_true", help="恢复时允许覆盖备份之后的新改动")
    p.add_argument("--prune-baseline", action="store_true", help="清理已无对应内容的基线")

    cmd("compat", "列出跨平台兼容提示（不修改任何文件）")
    return parser


_COMMON = None


def _common_parent() -> argparse.ArgumentParser:
    """子命令也接受 --config/--json（与全局位置等价）。"""
    global _COMMON
    if _COMMON is None:
        parent = argparse.ArgumentParser(add_help=False)
        _add_common(parent)
        _COMMON = parent
    return _COMMON


# ---------------------------------------------------------------------- 输出


def _exit_for(status: str) -> int:
    if status == STATUS_OK:
        return EXIT_OK
    if status == STATUS_NEEDS_ACTION:
        return EXIT_NEEDS_ACTION
    return EXIT_FAILED


def _bullet(lines: list[str], items: list[str], prefix: str = "  ") -> None:
    for item in items:
        lines.append(prefix + item)


def _render_plan(data: dict) -> list[str]:
    lines = [f"设备: {data.get('device')}    共享库: {data.get('hub')}"]
    if data.get("stale"):
        lines.append("远端检查: 未确认（本次未成功获取远端；此计划不能当作最新）")
    else:
        lines.append("远端检查: 已获取远端最新状态")
    if data.get("remote_error"):
        lines.append(f"远端错误: {data['remote_error']}")
    summary = data.get("summary") or {}
    lines.append(
        "计划: 共 {total} 项；需处理 {needs_action} 项；"
        "推送 {push} / 拉取 {deploy} / 合并 {merge} / 冲突 {conflict} / 待批准 {pending_add} / 错误 {error}".format(
            total=summary.get("total", 0),
            needs_action=summary.get("needs_action", 0),
            push=summary.get("push", 0) + summary.get("push_new", 0),
            deploy=summary.get("deploy", 0),
            merge=summary.get("merge", 0),
            conflict=summary.get("conflict", 0),
            pending_add=summary.get("pending_add", 0),
            error=summary.get("error", 0),
        )
    )
    labels = {
        "push": "推送", "push_new": "新增推送", "deploy": "拉取部署", "merge": "自动合并",
        "adopt": "补齐基线", "delete_local": "传播删除", "mark_uninstalled": "记录本机卸载",
        "pending_add": "待批准", "conflict": "冲突", "error": "错误", "unchanged": "未变化",
        "skip_local_only": "仅本机", "skip_uninstalled": "已卸载",
    }
    for item in data.get("items") or []:
        action = item.get("action", "")
        if action == "unchanged":
            continue
        mark = "!" if item.get("needs_action") else " "
        label = labels.get(action, action)
        line = f"{mark} [{label}] {item.get('id')}"
        if item.get("detail"):
            line += f" — {item['detail']}"
        if item.get("options"):
            line += f"（可选: {', '.join(item['options'])}）"
        lines.append(line)
        if item.get("compat"):
            lines.append(f"    兼容提示 {item['compat']} 条（详见 skills-sync compat）")
    if data.get("warnings"):
        lines.append("提醒:")
        _bullet(lines, list(data["warnings"]))
    if data.get("needs_action"):
        lines.append(f"需要人工处理: {', '.join(data['needs_action'])}")
    return lines


def _render_sync(data: dict) -> list[str]:
    lines = [f"操作: {data.get('op_id')}    状态: {data.get('status')}"]
    for key, label in (
        ("pushed", "已推送"), ("deployed", "已部署"), ("merged", "已合并"),
        ("adopted", "已补基线"), ("deleted_local", "已移除本机副本"),
        ("marked_uninstalled", "已记录本机卸载"), ("pending", "待批准"),
        ("conflicts", "冲突"), ("stale", "计划后又有改动（未覆盖）"),
        ("failures", "失败"), ("errors", "错误"),
    ):
        values = data.get(key) or []
        if not values:
            continue
        if key in ("conflicts", "stale", "failures", "errors"):
            rendered = []
            for value in values:
                if isinstance(value, dict):
                    rendered.append(str(value.get("skill_id") or value.get("id") or "") + (
                        f" — {value.get('detail') or value.get('message') or ''}".rstrip(" —")))
                else:
                    rendered.append(str(value))
            _bullet(lines, rendered, prefix=f"  {label}: ")
        else:
            _bullet(lines, [f"{label}: {', '.join(str(v) for v in values)}"])
    if data.get("head"):
        lines.append(f"共享库提交: {data['head']}")
    if data.get("notices"):
        lines.append("说明:")
        _bullet(lines, list(data["notices"]))
    if data.get("failures"):
        lines.append("部分完成：失败项已回滚/保留备份，请按提示处理后再同步。")
    return lines


def _render_doctor(data: dict) -> list[str]:
    py = data.get("python") or {}
    lines = [
        f"Python: {py.get('version')} ({py.get('executable')})",
        f"平台: {py.get('platform')}",
    ]
    git = data.get("git") or {}
    lines.append(f"Git: {git.get('version') or '未找到'}")
    config = data.get("config") or {}
    lines.append(f"配置: {config.get('path')}")
    hub = data.get("hub") or {}
    lines.append(f"共享库: {hub.get('path')} (仓库: {'是' if hub.get('repo') else '否'})")
    lines.append(f"状态目录: {(data.get('state') or {}).get('path')}")
    for root in data.get("roots") or []:
        flag = "ok" if root.get("exists") else "缺失"
        lines.append(f"  root {root.get('key')}: {root.get('path')} ({flag})")
    remote = data.get("remote") or {}
    if remote.get("url"):
        lines.append(f"远端: {remote.get('url')} — {'可达' if remote.get('ok') else '不可达'}")
    if data.get("warnings"):
        lines.append("提醒:")
        _bullet(lines, list(data["warnings"]))
    return lines


def _render_status(data: dict) -> list[str]:
    lines = [
        f"设备: {data.get('device')}",
        f"配置: {data.get('config')}",
        f"共享库: {data.get('hub')} (就绪: {'是' if data.get('hub_ready') else '否'})",
        f"远端: {data.get('remote_url') or '未配置（仅本机）'} ({data.get('branch')})",
    ]
    conflicts = data.get("conflicts") or []
    lines.append(f"冲突: {len(conflicts)}")
    for entry in conflicts:
        state = "已解决待 continue" if entry.get("resolved") else "待解决"
        lines.append(f"  - {entry.get('skill_id')} [{entry.get('kind')}] {state}")
    if data.get("pending_additions"):
        _bullet(lines, [f"待批准: {', '.join(data['pending_additions'])}"])
    if data.get("uninstalled"):
        _bullet(lines, [f"本机已卸载: {', '.join(data['uninstalled'])}"])
    if data.get("local_only"):
        _bullet(lines, [f"仅本机: {', '.join(data['local_only'])}"])
    recent = data.get("recent_ops") or []
    if recent:
        lines.append("最近操作:")
        for op in recent:
            lines.append(f"  - {op.get('op_id')} {op.get('kind')} {op.get('status')} {op.get('started_at')}")
    return lines


def _render_recover(data: dict) -> list[str]:
    lines = [
        f"共享库: {data.get('hub')} (仓库: {'是' if data.get('hub_repo') else '否'})",
        f"本地 HEAD: {data.get('hub_head')}",
        f"远端 HEAD: {data.get('remote_head')}",
    ]
    if data.get("dirty"):
        lines.append(f"未提交改动: {len(data['dirty'])} 个文件")
        _bullet(lines, [str(x) for x in data["dirty"][:20]])
    if data.get("conflicts"):
        lines.append("冲突:")
        for c in data["conflicts"]:
            lines.append(f"  - {c['skill_id']} [{c['kind']}] {c['conflict_id']}"
                         f" {'已解决' if c.get('resolved') else '待解决'}")
    if data.get("incomplete_ops"):
        _bullet(lines, [f"未完成操作: {', '.join(data['incomplete_ops'])}"])
    for backup in data.get("backups") or []:
        lines.append(f"备份 {backup['op_id']}: {', '.join(backup['skills'])}")
    if data.get("warnings"):
        lines.append("提醒:")
        _bullet(lines, list(data["warnings"]))
    return lines


def _render_compat(data: dict) -> list[str]:
    lines = ["兼容提示（文件会完整同步，但不代表跨平台可直接运行）:"]
    counts = data.get("counts") or {}
    if counts:
        lines.append("  " + "；".join(f"{k}: {v}" for k, v in sorted(counts.items())))
    for skill in data.get("skills") or []:
        lines.append(f"- {skill.get('id')} ({skill.get('root')})")
        for finding in skill.get("findings") or []:
            lines.append(f"    {finding.get('file')}:{finding.get('line')} [{finding.get('code')}] "
                         f"{finding.get('message')}")
    for note in data.get("notes") or []:
        lines.append(note)
    return lines


def _render_generic(command: str, data: dict) -> list[str]:
    if command == "init":
        lines = [
            f"配置: {data.get('config')}",
            f"设备: {data.get('device')}",
            f"共享库: {data.get('hub')}",
            f"状态目录: {data.get('state')}",
            f"远端: {data.get('remote_url') or '未配置（仅本机）'} ({data.get('branch')})",
            "skill 根:",
        ]
        for root in data.get("roots") or []:
            lines.append(f"  - {root.get('key')}: {root.get('path')}")
        if data.get("pushed"):
            lines.append("初始提交已推送。")
        elif data.get("initial_commit"):
            lines.append("初始提交已创建（未推送）。")
        if data.get("warnings"):
            _bullet(lines, list(data["warnings"]))
        if data.get("next"):
            lines.append(data["next"])
        return lines
    if command == "include":
        lines = []
        if data.get("approved"):
            lines.append(f"已批准: {', '.join(data['approved'])}")
        if data.get("local_only"):
            lines.append(f"已标记仅本机: {', '.join(data['local_only'])}")
        if data.get("next"):
            lines.append(data["next"])
        return lines or ["没有变化。"]
    if command == "uninstall":
        return [f"已在本机卸载: {', '.join(data.get('uninstalled') or [])}", data.get("note", "")]
    if command == "restore":
        lines = [f"已恢复: {', '.join(data.get('restored') or [])}"]
        for sid, path in (data.get("paths") or {}).items():
            lines.append(f"  - {sid} -> {path}")
        return lines
    if command == "delete-shared":
        lines = [f"已全局删除: {', '.join(data.get('deleted') or [])}"]
        if data.get("commit"):
            lines.append(f"提交: {data['commit']}")
        lines.append(data.get("note", ""))
        return lines
    if command == "resolve":
        lines = []
        for entry in data.get("resolved") or []:
            lines.append(f"{entry['skill_id']}: 选择 {entry['take']} ({entry['files']} 个文件, {entry['hash'][:12]})")
            if entry.get("next"):
                lines.append(f"  {entry['next']}")
        return lines
    if command == "continue":
        return [f"已应用: {', '.join(data.get('applied') or []) or '无'}"]
    if command == "abort":
        return [f"已放弃冲突: {', '.join(data.get('aborted') or []) or '无'}", data.get("note", "")]
    keys = [k for k in ("message", "note", "next") if data.get(k)]
    lines = []
    for key in keys:
        lines.append(str(data[key]))
    if not lines:
        lines.append("完成。")
    return lines


def _render(command: str, data: dict) -> list[str]:
    renderer = {
        "plan": _render_plan,
        "sync": _render_sync,
        "doctor": _render_doctor,
        "status": _render_status,
        "recover": _render_recover,
        "compat": _render_compat,
    }.get(command)
    if renderer:
        return renderer(data)
    return _render_generic(command, data)


# ---------------------------------------------------------------------- 分发


def _load_engine(args) -> Engine:
    cfg = config_mod.Config.load(Path(args.config) if getattr(args, "config", None) else None)
    return Engine(cfg)


def _init_command(args) -> tuple[dict, str]:
    path = Path(args.config) if getattr(args, "config", None) else config_mod.default_config_path()
    if path.exists() and not args.force:
        raise UsageError(
            f"配置文件已存在：{path}",
            hint="如需重建请加 --force（不会删除已有状态目录）；日常使用直接用 plan/sync。",
        )
    roots = []
    for spec in args.root or []:
        if "=" not in spec:
            raise UsageError(f"--root 格式应为 KEY=PATH，收到 {spec!r}")
        key, raw = spec.split("=", 1)
        key = key.strip()
        root_path = util.expand_path(raw.strip())
        if not key or not str(raw).strip():
            raise UsageError(f"--root 格式应为 KEY=PATH，收到 {spec!r}")
        roots.append({"key": key, "path": str(root_path)})
    if not roots:
        roots = config_mod.guess_roots()
    if not roots:
        raise UsageError(
            "没有找到用户 skill 根（~/.codex/skills、~/.agents/skills 都不存在）。",
            hint="用 --root codex=<路径> 显式指定，可重复多次。",
        )
    cfg = config_mod.write_default_config(
        path,
        remote_url=(args.remote or "").strip(),
        roots=roots,
        default_root=args.default_root,
        hub_dir=args.hub,
        state_dir=args.state,
    )
    if args.branch and args.branch != cfg.branch:
        cfg.data["branch"] = args.branch
        cfg.branch = args.branch
    if args.name:
        cfg.data["device_name"] = args.name
        cfg.device_name = args.name
    cfg.save()
    data = Engine(cfg).init()
    return data, "init"


def _dispatch(args) -> dict:
    command = args.command
    if command == "init":
        return _init_command(args)[0]
    engine = _load_engine(args)
    if command == "doctor":
        return engine.doctor()
    if command == "status":
        return engine.status()
    if command == "plan":
        return engine.plan(fetch=not args.offline)
    if command == "sync":
        return engine.sync(dry_run=args.dry_run)
    if command == "include":
        return engine.include(args.ids, all_pending=args.all_pending, exclude=args.exclude,
                              root=args.root)
    if command == "uninstall":
        return engine.uninstall(args.ids, yes=args.yes, dry_run=args.dry_run)
    if command == "restore":
        return engine.restore(args.ids, dry_run=args.dry_run, force=args.force)
    if command == "delete-shared":
        return engine.delete_shared(args.ids, yes=args.yes)
    if command == "resolve":
        return engine.resolve(args.ids, take=args.take, directory=args.dir)
    if command == "continue":
        return engine.continue_(args.ids or None)
    if command == "abort":
        return engine.abort(args.ids or None)
    if command == "recover":
        return engine.recover(restore=args.restore, force=args.force,
                              prune_baseline=args.prune_baseline)
    if command == "compat":
        return engine.compat()
    raise UsageError(f"未知命令：{command!r}", hint="运行 skills-sync --help 查看可用命令。")


def _emit(payload: dict, as_json: bool) -> None:
    # 统一出口脱敏：任何命令的 JSON/文本输出都不得回显 URL 中的凭据
    payload = util.redact_data(payload)
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        for line in payload.get("lines") or []:
            print(line)


def _payload(command: str, data: dict) -> dict:
    status = data.get("status") or STATUS_OK
    return {
        "ok": status == STATUS_OK,
        "status": status,
        "exit_code": _exit_for(status),
        "command": command,
        "data": data,
        "lines": _render(command, data),
    }


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):  # pragma: no cover - 非标准流
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return EXIT_OK
    as_json = bool(getattr(args, "json", False))
    command = args.command
    try:
        data = _dispatch(args)
    except SyncError as exc:
        payload = {
            "ok": False,
            "status": getattr(exc, "status", STATUS_FAILED),
            "exit_code": exc.exit_code,
            "command": command,
            "data": None,
            "error": exc.message,
            "hint": exc.hint,
            "details": exc.details or {},
            "lines": [f"失败：{exc.message}"]
            + ([f"提示：{exc.hint}"] if exc.hint else []),
        }
        _emit(payload, as_json)
        return exc.exit_code
    except Exception as exc:  # 兜底：不把 traceback 当正常输出
        payload = {
            "ok": False,
            "status": STATUS_FAILED,
            "exit_code": EXIT_FAILED,
            "command": command,
            "data": None,
            "error": f"{type(exc).__name__}: {exc}",
            "hint": "这是未预期的错误，请保留输出以便排查。",
            "lines": [f"失败：{type(exc).__name__}: {exc}"],
        }
        _emit(payload, as_json)
        return EXIT_FAILED
    payload = _payload(command, data)
    _emit(payload, as_json)
    return payload["exit_code"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
