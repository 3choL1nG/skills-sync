"""skill 扫描：发现、过滤、凭据检查、来源记录、跨平台兼容提示。"""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path

from . import util

LICENSE_NAMES = {
    "license", "license.md", "license.txt", "licence", "licence.md",
    "copying", "copying.txt", "notice", "notice.txt", "authors", "third_party_notices.md",
}

SECRET_NAME_ALLOW = {".env.example", ".env.sample", ".env.template"}
SYSTEM_DIR_NAMES = {".system", ".git", ".github", ".svn", ".hg", ".plugins"}

COMPAT_RULES = [
    ("win_path", re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/]"),
     "Windows 绝对路径；在 macOS 上不可用"),
    ("posix_path", re.compile(r"(?<![\w.-])/(?:Users|home|opt/homebrew|usr/local)/"),
     "POSIX 绝对路径；在 Windows 上不可用"),
    ("powershell_invocation", re.compile(r"\b(?:powershell(?:\.exe)?|pwsh(?:\.exe)?)\b", re.IGNORECASE),
     "PowerShell 调用；macOS 需另装 pwsh"),
    ("mac_only_tool", re.compile(r"\b(?:osascript|pbcopy|pbpaste)\b|\bbrew\s+(?:install|update|list)\b|\bopen\s+-a\b"),
     "macOS 专用工具；Windows 不支持"),
    ("py_launcher", re.compile(r"(?<![\w-])py\s+-3(?!\d)"),
     "Windows py 启动器写法；macOS 通常用 python3"),
    ("bash_shebang", re.compile(r"^#!\s*/(?:usr/)?bin/(?:env\s+)?(?:ba|z|k)?sh\b", re.MULTILINE),
     "bash/zsh 脚本头；Windows 需 Git Bash 或 WSL"),
]


def glob_to_regex(pattern: str) -> re.Pattern:
    pattern = pattern.replace("\\", "/")
    i = 0
    out = []
    while i < len(pattern):
        ch = pattern[i]
        if ch == "*":
            if pattern[i:i + 3] == "**/":
                out.append("(?:.*/)?")
                i += 3
                continue
            if pattern[i:i + 2] == "**":
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
            i += 1
            continue
        if ch == "?":
            out.append("[^/]")
            i += 1
            continue
        if ch == "[":
            j = pattern.find("]", i + 1)
            if j == -1:
                out.append(re.escape(ch))
                i += 1
                continue
            body = pattern[i + 1:j]
            if body.startswith("!"):
                body = "^" + body[1:]
            out.append("[" + body + "]")
            i = j + 1
            continue
        out.append(re.escape(ch))
        i += 1
    return re.compile("^" + "".join(out) + "$")


class Filters:
    """路径过滤：排除规则 + 凭据文件名规则。"""

    def __init__(self, cfg):
        self.exclude = [(pat, glob_to_regex(pat)) for pat in cfg.exclude_globs()]
        self.secret_names = [(pat, glob_to_regex(pat)) for pat in cfg.secret_name_globs()]
        self.secret_patterns = []
        for raw in cfg.secret_content_patterns():
            try:
                self.secret_patterns.append((raw, re.compile(raw)))
            except re.error as exc:
                raise ValueError(f"配置中的秘密检测正则无效：{raw!r}（{exc}）") from exc

    def excluded(self, rel: str) -> str | None:
        rel = rel.replace("\\", "/")
        for pattern, rx in self.exclude:
            if rx.match(rel):
                return pattern
        return None

    def secret_name(self, rel: str) -> str | None:
        base = rel.rsplit("/", 1)[-1]
        if base.lower() in SECRET_NAME_ALLOW:
            return None
        for pattern, rx in self.secret_names:
            if rx.match(rel) or fnmatch.fnmatch(base, pattern.replace("**/", "")):
                return pattern
        return None


def scan_roots(cfg) -> dict:
    """扫描配置中的全部用户 skill 根，返回结构化结果（只读）。"""
    filters = Filters(cfg)
    result = {
        "roots": [],
        "skills": {},
        "skipped": [],
        "collisions": [],
        "duplicates": [],
        "same_name_conflicts": [],
        "filter_report": {"excluded_dirs": [], "excluded_files": [], "secret_files": []},
    }
    by_id: dict[str, list[dict]] = {}
    for root in cfg.roots:
        root_path = Path(root["path"])
        entry = {"key": root["key"], "path": str(root_path), "exists": root_path.is_dir()}
        result["roots"].append(entry)
        if not entry["exists"]:
            result["skipped"].append({"root": root["key"], "name": "", "reason": "root-missing"})
            continue
        for child in sorted(root_path.iterdir(), key=lambda p: p.name):
            if not child.is_dir():
                continue
            kind = util.link_kind(child)
            if kind:
                result["skipped"].append({
                    "root": root["key"], "name": child.name,
                    "reason": f"{kind}-root-entry",
                })
                continue
            if child.name in SYSTEM_DIR_NAMES or child.name.startswith("."):
                result["skipped"].append({"root": root["key"], "name": child.name, "reason": "system-or-hidden"})
                continue
            if not _looks_like_skill(child):
                result["skipped"].append({"root": root["key"], "name": child.name, "reason": "no-SKILL.md"})
                continue
            scanned = scan_skill(child, filters, skill_id=util.normalize_identity(child.name))
            scanned["root"] = root["key"]
            scanned["path"] = str(child)
            scanned["name"] = child.name
            if not scanned["files"]:
                result["skipped"].append({"root": root["key"], "name": child.name, "reason": "empty-after-filter"})
                continue
            result["skills"][f"{scanned['id']}@{root['key']}"] = scanned
            by_id.setdefault(scanned["id"], []).append(scanned)
            result["filter_report"]["excluded_dirs"].extend(
                {"root": root["key"], "skill": child.name, **item} for item in scanned["excluded_dirs"]
            )
            result["filter_report"]["excluded_files"].extend(
                {"root": root["key"], "skill": child.name, **item} for item in scanned["secret_files"]
            )

    # 同名（跨根）与大小写/Unicode 碰撞
    for skill_id, entries in by_id.items():
        raw_names = {e["name"] for e in entries}
        if len(entries) > 1:
            if len(raw_names) > 1:
                result["collisions"].append({
                    "id": skill_id,
                    "names": sorted(raw_names),
                    "roots": [e["root"] for e in entries],
                    "reason": "case-or-unicode-collision",
                })
                continue
            hashes = {e["hash"] for e in entries}
            if len(hashes) == 1:
                result["duplicates"].append({
                    "id": skill_id,
                    "name": entries[0]["name"],
                    "roots": [e["root"] for e in entries],
                    "hash": entries[0]["hash"],
                })
            else:
                result["same_name_conflicts"].append({
                    "id": skill_id,
                    "name": entries[0]["name"],
                    "entries": [
                        {
                            "root": e["root"],
                            "path": e["path"],
                            "dir_name": e["name"],
                            "hash": e["hash"],
                            # 冻结候选内容所需：真实过滤后的文件清单（不含缓存/凭据）
                            "files": dict(e["files"]),
                        }
                        for e in entries
                    ],
                })
    return result


def _looks_like_skill(path: Path) -> bool:
    return (path / "SKILL.md").is_file() or (path / "skill.md").is_file()


def scan_skill(skill_dir: Path, filters: Filters, skill_id: str) -> dict:
    """扫描单个 skill 目录；不跟随符号链接，越界链接只记录不读取。"""
    root = Path(skill_dir)
    files: dict[str, dict] = {}
    excluded_dirs: list[dict] = []
    secret_files: list[dict] = []
    links: list[dict] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(dirpath)
        relbase = "" if base == root else str(base.relative_to(root)).replace("\\", "/")
        keep = []
        for name in sorted(dirnames):
            rel = f"{relbase}/{name}" if relbase else name
            full = base / name
            kind = util.link_kind(full)
            if kind:
                links.append({"path": rel, "reason": f"{kind}-dir"})
                continue
            pattern = filters.excluded(rel + "/")
            if pattern:
                excluded_dirs.append({"path": rel, "reason": f"exclude:{pattern}"})
                continue
            keep.append(name)
        dirnames[:] = keep
        for name in sorted(filenames):
            rel = f"{relbase}/{name}" if relbase else name
            full = base / name
            kind = util.link_kind(full)
            if kind:
                links.append({"path": rel, "reason": f"{kind}-file"})
                continue
            pattern = filters.excluded(rel)
            if pattern:
                excluded_dirs.append({"path": rel, "reason": f"exclude:{pattern}"})
                continue
            secret_name = filters.secret_name(rel)
            if secret_name:
                secret_files.append({"path": rel, "reason": f"secret-name:{secret_name}"})
                continue
            try:
                size = full.stat().st_size
            except OSError:
                continue
            # 列举之后、读取之前仍可能被换成 junction：哈希前逐级核验边界与链接
            try:
                util.guard_path(root, full, "扫描文件")
            except ValueError as exc:
                links.append({"path": rel, "reason": f"unsafe-path:{exc}"})
                continue
            files[rel] = {"hash": util.sha256_file(full), "size": size}

    # 内容级凭据检查：命中即排除并要求用户确认
    text_cache: dict[str, str] = {}
    for rel in sorted(list(files)):
        full = root / rel
        try:
            util.guard_path(root, full, "读取文件")
        except ValueError:
            files.pop(rel, None)
            links.append({"path": rel, "reason": "unsafe-path-on-read"})
            continue
        text = util.read_text(full)
        if text is None:
            continue
        text_cache[rel] = text
        hit = None
        for raw, rx in filters.secret_patterns:
            if rx.search(text):
                hit = raw
                break
        if hit:
            files.pop(rel, None)
            secret_files.append({"path": rel, "reason": f"secret-content:{hit}"})

    source = detect_source(root)
    licenses = sorted(rel for rel in files if rel.rsplit("/", 1)[-1].lower() in LICENSE_NAMES)
    compat = detect_compat(root, files, text_cache)
    return {
        "id": skill_id,
        "dir_name": root.name,
        "files": files,
        "hash": util.tree_hash(files),
        "excluded_dirs": excluded_dirs,
        "secret_files": secret_files,
        "links": links,
        "source": source,
        "licenses": licenses,
        "compat": compat,
    }


def detect_source(root: Path) -> dict:
    """尽力记录来源；未知不要编造（R-02/R-12）。"""
    meta: dict = {"type": "unknown", "url": "", "version": "", "evidence": []}
    if (root / ".git").exists():
        meta["type"] = "git-clone-metadata"
        meta["evidence"].append(".git 目录存在（不纳入同步）")
    for candidate in ("BUILD_MANIFEST.json", "SKILL.md", "skill.json", "package.json"):
        path = root / candidate
        if not path.is_file():
            continue
        try:
            util.guard_path(root, path, "读取来源声明")
        except ValueError:
            continue
        text = util.read_text(path, limit=256 * 1024)
        if not text:
            continue
        url = re.search(r"https?://[^\s\"'<>)]+", text)
        ver = re.search(r"(?im)^\s*\"?version\"?\s*[:=]\s*\"?([0-9][0-9A-Za-z.\-+]{0,30})", text)
        if url and not meta["url"]:
            meta["url"] = url.group(0)
            meta["evidence"].append(candidate)
            # 有明确声明来源时，以声明为准；.git 仅作为证据保留
            meta["type"] = "declared"
        if ver:
            meta["version"] = ver.group(1)
    if meta["type"] == "unknown":
        meta["note"] = "未发现可识别的安装来源记录，标为未知；不依赖外部锁文件推断"
    return meta


def detect_compat(root: Path, files: dict, text_cache: dict[str, str]) -> list[dict]:
    """只读扫描跨平台风险，不修改任何业务文件。"""
    findings = []
    for rel in sorted(files):
        low = rel.lower()
        if low.endswith(".ps1"):
            findings.append({"code": "powershell_script", "file": rel, "line": 0,
                             "message": "PowerShell 脚本；macOS 需 pwsh 才能运行"})
            continue
        if low.endswith((".sh", ".bash", ".zsh")):
            findings.append({"code": "shell_script", "file": rel, "line": 0,
                             "message": "Shell 脚本；Windows 需 Git Bash 或 WSL"})
        text = text_cache.get(rel)
        if text is None:
            if rel.lower().endswith((".cmd", ".bat")):
                findings.append({"code": "cmd_script", "file": rel, "line": 0,
                                 "message": "Windows 批处理；macOS 不可直接运行"})
            continue
        for code, rx, message in COMPAT_RULES:
            for match in rx.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                snippet = text.splitlines()[line - 1].strip()[:160] if text.splitlines() else ""
                findings.append({"code": code, "file": rel, "line": line, "message": message, "text": snippet})
                break
        if len(findings) > 200:
            findings.append({"code": "truncated", "file": rel, "line": 0,
                             "message": "兼容性提示过多，已截断；请抽查"})
            break
    return findings


def scan_hub(hub: Path):
    """扫描共享库 skills/ 目录，返回 {id: {files, hash, path}}。"""
    skills_dir = Path(hub) / "skills"
    out = {}
    if not skills_dir.is_dir():
        return out
    for child in sorted(skills_dir.iterdir(), key=lambda p: p.name):
        if not child.is_dir() or util.is_link_like(child):
            continue
        files = util.snapshot_dir(child)
        out[child.name] = {
            "id": child.name,
            "files": files,
            "hash": util.tree_hash(files),
            "path": str(child),
        }
    return out
