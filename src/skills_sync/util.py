"""通用工具：路径、哈希、快照、JSON 持久化、原子替换。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

BLOCK_SIZE = 1024 * 1024
TEXT_PROBE_LIMIT = 512 * 1024


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def expand_path(value: str, base: Path | None = None) -> Path:
    """展开 ~ 与环境变量；相对路径相对 base（默认当前目录）。"""
    if value is None:
        raise ValueError("path value must not be None")
    text = os.path.expandvars(os.path.expanduser(str(value)))
    p = Path(text)
    if not p.is_absolute():
        p = (base or Path.cwd()) / p
    return p


def resolve_real(path: Path) -> Path:
    """规范化绝对路径（解析符号链接与 ..）；不存在的路径也可处理。"""
    try:
        return Path(os.path.realpath(str(path)))
    except OSError:
        return Path(os.path.abspath(str(path)))


def same_path(a: Path, b: Path) -> bool:
    return _norm_case(str(resolve_real(a))) == _norm_case(str(resolve_real(b)))


def _norm_case(text: str) -> str:
    return os.path.normcase(os.path.normpath(text))


def is_within(child: Path, parent: Path) -> bool:
    """child 是否位于 parent 之内（含相等）。拒绝 .. 逃逸。"""
    c = _norm_case(str(resolve_real(child)))
    p = _norm_case(str(resolve_real(parent)))
    if c == p:
        return True
    return c.startswith(p.rstrip("\\/") + os.sep)


def require_within(child: Path, parent: Path, what: str = "path") -> None:
    if not is_within(child, parent):
        raise ValueError(f"{what} 越界：{child} 不在允许根 {parent} 之内")


_REPARSE_FLAG = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def is_reparse(path: Path) -> bool:
    """是否为 Windows reparse point（junction / mount point / 其他重解析点）。

    `Path.is_symlink()` 对 junction 返回 False，因此不能只用它判断链接。
    平台不支持或无法判定时按“可能是链接”处理（安全拒绝），除非明确不是。
    """
    p = Path(path)
    try:
        st = os.lstat(p)
    except OSError:
        return False
    attrs = getattr(st, "st_file_attributes", 0)
    if attrs & _REPARSE_FLAG:
        return True
    # 非 Windows 平台没有该属性；POSIX 下由 is_symlink 覆盖。
    return False


def link_kind(path: Path) -> str:
    """返回链接类型：symlink / junction / reparse / ""（普通路径）。"""
    p = Path(path)
    try:
        if p.is_symlink():
            return "symlink"
    except OSError:
        return "reparse"
    if not is_reparse(p):
        return ""
    isjunction = getattr(os.path, "isjunction", None)
    if callable(isjunction):
        try:
            if isjunction(p):
                return "junction"
        except OSError:
            pass
    return "reparse"


def is_link_like(path: Path) -> bool:
    """符号链接或 Windows junction/reparse point（不跟随、不读取内容）。"""
    return bool(link_kind(path))


def require_no_link_chain(child: Path, parent: Path, what: str = "path") -> None:
    """确保 child 到 parent 之间（含 child 自身）没有链接/reparse point。

    仅比较 realpath 不足以说明“用户看到的就是被复制的”：中间父目录若是
    junction，realpath 会解析成功但写入/读取仍可能落在根外。这里逐级显式检查。
    """
    child = Path(child)
    parent = Path(parent)
    try:
        rel = child.relative_to(parent)
    except ValueError:
        rel = None
    if rel is None:
        require_within(child, parent, what)
        return
    current = parent
    for part in rel.parts:
        current = current / part
        kind = link_kind(current)
        if kind:
            raise ValueError(f"{what} 经过{kind}链接，拒绝跟随：{current}")


def guard_path(root: Path, target: Path, what: str = "path") -> Path:
    """读取/复制/写入前的统一边界核验。

    两道检查缺一不可：
    1. `require_within`：解析 realpath 后必须仍在 root 之内（挡住指向根外的链接）；
    2. `require_no_link_chain`：root 到 target 的每一级都不得是链接/reparse point
       （挡住"解析后仍在根内但实际读写落在别处"的中间 junction）。
    """
    root = Path(root)
    target = Path(target)
    require_within(target, root, what)
    require_no_link_chain(target, root, what)
    return target


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(BLOCK_SIZE), b""):
            h.update(chunk)
    return h.hexdigest()


def normalize_rel(rel: str) -> str:
    rel = rel.replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    return rel


def list_tree(root: Path) -> list[tuple[str, Path]]:
    """列出目录下所有普通文件（不跟随符号链接/junction），返回 (相对路径, 绝对路径)。

    符号链接与 Windows reparse point 既不跟随也不纳入；由调用方决定是否报告。
    """
    root = Path(root)
    out: list[tuple[str, Path]] = []
    if not root.exists() or is_link_like(root):
        return out
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(dirpath)
        dirnames.sort()
        filenames.sort()
        dirnames[:] = [name for name in dirnames if not is_link_like(base / name)]
        for name in filenames:
            full = base / name
            if is_link_like(full):
                continue
            rel = normalize_rel(str(full.relative_to(root)))
            out.append((rel, full))
    return out


def audit_tree(root: Path) -> list[tuple[str, Path]]:
    """列出文件并逐级核验边界与链接（读取哈希、复制前的审计入口）。

    `list_tree` 已排除链接目录，这里再对每个将要读取/复制的文件核验一次，
    覆盖"列举之后、读取之前"被换成 junction 的情况。
    """
    root = Path(root)
    out: list[tuple[str, Path]] = []
    for rel, full in list_tree(root):
        guard_path(root, full, "目录内容")
        out.append((rel, full))
    return out


def snapshot_dir(root: Path, exclude=None) -> dict[str, dict]:
    """为目录生成内容快照：{相对路径: {hash,size}}。

    exclude: callable(rel_path) -> bool，返回 True 表示排除。
    每个文件的哈希读取都先经过 `audit_tree` 的边界/链接核验。
    """
    files: dict[str, dict] = {}
    for rel, full in audit_tree(root):
        if exclude and exclude(rel):
            continue
        try:
            st = full.stat()
        except OSError:
            continue
        files[rel] = {"hash": sha256_file(full), "size": st.st_size}
    return files


def tree_hash(files: dict[str, dict]) -> str:
    """快照聚合哈希：与顺序、平台路径分隔符无关。"""
    h = hashlib.sha256()
    for rel in sorted(files):
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(str(files[rel].get("hash", "")).encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()


def snapshot_hash_dir(root: Path, exclude=None) -> tuple[dict, str]:
    files = snapshot_dir(root, exclude=exclude)
    return files, tree_hash(files)


def copy_tree(src: Path, dst: Path) -> None:
    """复制目录内容（不复制符号链接/junction），dst 必须不存在。"""
    src = Path(src)
    dst = Path(dst)
    if is_link_like(src):
        raise ValueError(f"源目录是链接/reparse point，拒绝复制：{src}")
    if dst.exists() or is_link_like(dst):
        raise FileExistsError(f"目标已存在：{dst}")
    dst.mkdir(parents=True)
    if not src.exists():
        return
    for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
        base = Path(dirpath)
        relbase = base.relative_to(src)
        dirnames.sort()
        filenames.sort()
        dirnames[:] = [name for name in dirnames if not is_link_like(base / name)]
        target_dir = dst / relbase
        guard_path(dst, target_dir, "复制目标目录")
        target_dir.mkdir(parents=True, exist_ok=True)
        for name in filenames:
            full = base / name
            if is_link_like(full):
                continue
            guard_path(src, full, "复制源")
            copy_file(full, target_dir / name, source_root=src, target_root=dst)


def copy_file(src: Path, dst: Path, *, source_root: Path | None = None,
              target_root: Path | None = None) -> None:
    """Copy only after validating both paths, including destination parents."""
    src, dst = Path(src), Path(dst)
    if source_root is not None:
        guard_path(source_root, src, "复制源")
    if target_root is not None:
        guard_path(target_root, dst, "复制目标")
    dst.parent.mkdir(parents=True, exist_ok=True)
    if source_root is not None:
        guard_path(source_root, src, "复制源")
    if target_root is not None:
        guard_path(target_root, dst, "复制目标")
    shutil.copy2(src, dst)


def copy_selected(src: Path, files: dict[str, dict], dst: Path) -> None:
    """只复制快照中列出的文件（已过滤缓存/凭据），保持相对路径。"""
    src = Path(src)
    dst = Path(dst)
    kind = link_kind(src)
    if kind:
        raise ValueError(f"复制源是{kind}链接，拒绝跟随：{src}")
    dst.mkdir(parents=True, exist_ok=True)
    for rel in sorted(files):
        if not safe_relpath(rel):
            raise ValueError(f"快照中包含非法相对路径：{rel!r}")
        source = src / Path(rel)
        if not source.is_file() or is_link_like(source):
            raise FileNotFoundError(f"源文件缺失或为链接：{source}")
        # 中间目录可能是 junction（解析后指向根外或仍指向根内），逐级核验后才复制
        guard_path(src, source, "复制源")
        copy_file(source, dst / Path(rel), source_root=src, target_root=dst)


def verify_snapshot(root: Path, files: dict[str, dict], exclude=None) -> list[str]:
    """校验目录内容与快照一致，返回问题列表（空表示一致）。"""
    actual = snapshot_dir(root, exclude=exclude)
    problems = []
    for rel, meta in files.items():
        if rel not in actual:
            problems.append(f"缺少文件：{rel}")
        elif actual[rel]["hash"] != meta.get("hash"):
            problems.append(f"内容不一致：{rel}")
    for rel in actual:
        if rel not in files:
            problems.append(f"多余文件：{rel}")
    return problems


def tree_equals(left: dict[str, dict], right: dict[str, dict]) -> bool:
    if set(left) != set(right):
        return False
    return all(left[k].get("hash") == right[k].get("hash") for k in left)


def atomic_write_text(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_json(path: Path, data) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def read_json(path: Path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取 JSON {path}: {exc}") from exc


def read_text(path: Path, limit: int = TEXT_PROBE_LIMIT) -> str | None:
    """尽力按 UTF-8 读取文本；二进制或过大返回 None。"""
    try:
        size = Path(path).stat().st_size
    except OSError:
        return None
    if size > limit:
        return None
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    if b"\0" in data:
        return None
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


_WIN_ABS = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/]")

_URL_USERINFO = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)(?P<userinfo>[^/@\s]+)@")


def url_has_credentials(url: str) -> bool:
    return bool(url) and bool(_URL_USERINFO.search(str(url)))


def redact_url(url: str) -> str:
    """把 URL 中的用户信息（可能是 token）替换为 ***，保留主机与路径。"""
    if not url:
        return ""
    return _URL_USERINFO.sub(lambda m: m.group("scheme") + "***@", str(url))


def redact_text(text: str) -> str:
    """对任意文本中的 URL userinfo 脱敏（Git 错误、日志等）。"""
    if not text:
        return ""
    return _URL_USERINFO.sub(lambda m: m.group("scheme") + "***@", str(text))


def redact_data(value):
    """递归脱敏 JSON 可序列化结构中的所有字符串。"""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {key: redact_data(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_data(item) for item in value]
    return value


def normalize_identity(name: str) -> str:
    """把目录名规范成稳定逻辑身份：Unicode 归一、大小写折叠、保留中英文。"""
    text = unicodedata.normalize("NFKC", name).strip()
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"[^\w.\-]+", "-", text, flags=re.UNICODE)
    text = text.strip(".-")
    text = re.sub(r"-{2,}", "-", text)
    return text.casefold()


def safe_relpath(rel: str) -> bool:
    """拒绝绝对路径、盘符、.. 等越界相对路径。"""
    rel = normalize_rel(rel)
    if not rel or rel.startswith("/"):
        return False
    if _WIN_ABS.match(rel):
        return False
    parts = rel.split("/")
    return all(part not in ("..", "") for part in parts)


def remove_dir(path: Path) -> None:
    path = Path(path)
    kind = link_kind(path)
    if kind in ("symlink", "junction"):
        # 只移除链接本身，绝不递归进入目标（junction 若 rmtree 会删掉目标内容）。
        try:
            path.unlink()
            return
        except OSError:
            os.rmdir(path)
            return
    if kind == "reparse":
        os.rmdir(path)
        return
    if path.exists():
        shutil.rmtree(path, onerror=_on_rm_error)


def _on_rm_error(func, path, exc_info):  # pragma: no cover - 平台相关兜底
    try:
        os.chmod(path, 0o700)
        func(path)
    except OSError:
        raise
