"""本机配置：路径、用户 skill 根、过滤规则。配置文件在状态目录之外也可独立存在。"""

from __future__ import annotations

import copy
import os
from pathlib import Path

from . import util
from .errors import UsageError

SCHEMA = 1
DEFAULT_CONFIG_PATH = Path("~/.skills-sync/config.json")

DEFAULT_EXCLUDE_GLOBS = [
    # VCS 元数据
    ".git", ".git/**", ".svn", ".svn/**", ".hg", ".hg/**",
    # 缓存与字节码
    "**/__pycache__/**", "**/*.pyc", "**/*.pyo", "**/*.pyd",
    "**/.pytest_cache/**", "**/.mypy_cache/**", "**/.ruff_cache/**",
    "**/.cache/**", "**/.tmp/**", "**/node_modules/**",
    "**/.venv/**", "**/venv/**",
    # 操作系统垃圾文件
    "**/.DS_Store", "**/Thumbs.db", "**/desktop.ini",
]

# 运行产物：仅排除 skill 根目录下这几个常见的"运行输出"目录，避免误删自带资源。
DEFAULT_RUNTIME_GLOBS = ["output/**", "out/**", "logs/**", "tmp/**"]

# 凭据/密钥类文件名：默认一律不纳入同步包。
DEFAULT_SECRET_NAME_GLOBS = [
    "**/.env", "**/.env.*", "**/*.pem", "**/*.key", "**/*.p12", "**/*.pfx",
    "**/id_rsa*", "**/id_ed25519*", "**/credentials*", "**/secrets*",
    "**/*.token", "**/*.secret", "**/.netrc", "**/.git-credentials",
]

# 高置信度令牌内容特征（仅用于提示与排除，不承诺覆盖所有秘密）。
DEFAULT_SECRET_CONTENT_PATTERNS = [
    r"ghp_[A-Za-z0-9]{20,}",
    r"github_pat_[A-Za-z0-9_]{20,}",
    r"AKIA[0-9A-Z]{16}",
    r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----",
    r"xox[baprs]-[A-Za-z0-9-]{10,}",
    r"sk-[A-Za-z0-9]{20,}",
]

DEFAULT_CONFIG = {
    "schema": SCHEMA,
    "device_name": "",
    "remote_url": "",
    "branch": "main",
    "hub_dir": "hub",
    "state_dir": "state",
    "roots": [],
    "default_root": "",
    "exclude_globs": DEFAULT_EXCLUDE_GLOBS + DEFAULT_RUNTIME_GLOBS,
    "secret_name_globs": DEFAULT_SECRET_NAME_GLOBS,
    "secret_content_patterns": DEFAULT_SECRET_CONTENT_PATTERNS,
    "compat": {"enabled": True},
    "git": {"timeout_seconds": 120, "author_name": "skills-sync", "author_email": "skills-sync@localhost"},
}


def default_config_path() -> Path:
    override = os.environ.get("SKILLS_SYNC_CONFIG")
    if override:
        return util.expand_path(override)
    return util.expand_path(str(DEFAULT_CONFIG_PATH))


def guess_roots() -> list[dict]:
    """根据当前用户主目录给出候选 skill 根；不存在的不写入配置。"""
    home = Path.home()
    candidates = [
        ("codex", home / ".codex" / "skills"),
        ("agents", home / ".agents" / "skills"),
    ]
    roots = []
    for key, path in candidates:
        if path.is_dir():
            roots.append({"key": key, "path": str(path)})
    return roots


class Config:
    def __init__(self, path: Path, data: dict):
        self.path = Path(path)
        self.data = data
        self.base_dir = self.path.parent
        self.hub_dir = util.expand_path(data["hub_dir"], self.base_dir)
        self.state_dir = util.expand_path(data["state_dir"], self.base_dir)
        self.branch = data.get("branch") or "main"
        self.remote_url = (data.get("remote_url") or "").strip()
        self.device_name = (data.get("device_name") or "").strip() or _hostname()
        self.roots = []
        for entry in data.get("roots", []):
            # 相对路径相对配置文件目录解析（与 hub_dir/state_dir 一致），便于整包换目录复跑
            p = util.expand_path(entry["path"], self.base_dir)
            self.roots.append({"key": entry["key"], "path": util.resolve_real(p)})
        self.default_root = data.get("default_root") or (self.roots[0]["key"] if self.roots else "")
        self.git_timeout = int(data.get("git", {}).get("timeout_seconds", 120))
        self.git_executable = (data.get("git", {}).get("executable") or "").strip()
        self.git_author_name = data.get("git", {}).get("author_name", "skills-sync")
        self.git_author_email = data.get("git", {}).get("author_email", "skills-sync@localhost")
        self.compat_enabled = bool(data.get("compat", {}).get("enabled", True))

    # ---------- 读写 ----------

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = Path(path) if path else default_config_path()
        if not path.exists():
            raise UsageError(
                f"找不到配置文件：{path}",
                hint="先运行 doctor，再用 init 生成配置：skills-sync init --remote <仓库URL>",
            )
        try:
            data = util.read_json(path)
        except ValueError as exc:
            raise UsageError(str(exc)) from exc
        merged = copy.deepcopy(DEFAULT_CONFIG)
        _deep_update(merged, data or {})
        missing = [k for k in ("roots",) if not merged.get(k)]
        if missing:
            raise UsageError(
                f"配置文件缺少必填项：{', '.join(missing)}",
                hint="参考 config.example.json 补全 roots（本机用户 skill 根目录）。",
            )
        cfg = cls(path, merged)
        cfg.validate()
        return cfg

    def validate(self) -> None:
        schema = self.data.get("schema")
        if schema != SCHEMA:
            raise UsageError(f"配置 schema 不支持：{schema!r}（当前支持 {SCHEMA}）")
        if not self.roots:
            raise UsageError("配置中没有 roots，无法扫描 skill。")
        keys = set()
        for root in self.roots:
            if not root["key"]:
                raise UsageError("roots 中存在空 key。")
            if root["key"] in keys:
                raise UsageError(f"roots 中 key 重复：{root['key']}")
            keys.add(root["key"])
        if self.default_root and self.default_root not in keys:
            raise UsageError(f"default_root={self.default_root!r} 不在 roots 中")

    def save(self) -> None:
        self.data["roots"] = [{"key": r["key"], "path": str(r["path"])} for r in self.roots]
        self.data["default_root"] = self.default_root
        util.write_json(self.path, self.data)

    # ---------- 查询 ----------

    def root_by_key(self, key: str) -> dict | None:
        for root in self.roots:
            if root["key"] == key:
                return root
        return None

    def root_for_path(self, path: Path) -> dict | None:
        for root in self.roots:
            if util.is_within(path, root["path"]) and not util.same_path(path, root["path"]):
                return root
        return None

    def root_keys(self) -> list[str]:
        return [r["key"] for r in self.roots]

    def exclude_globs(self) -> list[str]:
        return list(self.data.get("exclude_globs") or [])

    def secret_name_globs(self) -> list[str]:
        return list(self.data.get("secret_name_globs") or [])

    def secret_content_patterns(self) -> list[str]:
        return list(self.data.get("secret_content_patterns") or [])


def _hostname() -> str:
    try:
        import socket

        name = socket.gethostname().strip()
        return name or "unknown-device"
    except OSError:  # pragma: no cover
        return "unknown-device"


def _deep_update(base: dict, patch: dict) -> None:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_update(base[key], value)
        else:
            base[key] = value


def write_default_config(path: Path, *, remote_url: str, roots: list[dict], default_root: str = "",
                         hub_dir: str = "hub", state_dir: str = "state") -> Config:
    data = copy.deepcopy(DEFAULT_CONFIG)
    data.update(
        {
            "remote_url": remote_url,
            "hub_dir": hub_dir,
            "state_dir": state_dir,
            "roots": [{"key": r["key"], "path": str(r["path"])} for r in roots],
            "default_root": default_root or (roots[0]["key"] if roots else ""),
        }
    )
    util.write_json(path, data)
    return Config.load(path)
