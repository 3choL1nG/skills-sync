"""Git 子命令封装：fetch/commit/push/reset 与仓库初始化。

只调用系统 git，不修改全局 Git 配置；提交身份用 -c 临时指定。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from . import util
from .errors import GitError


class Git:
    def __init__(self, cfg):
        self.cfg = cfg
        self.timeout = cfg.git_timeout
        self.env = dict(os.environ)
        # 避免交互式凭据提示卡死；沿用用户已有凭据助手。
        self.env.setdefault("GIT_TERMINAL_PROMPT", "0")
        self.executable = self._resolve_executable(cfg)

    @staticmethod
    def _resolve_executable(cfg) -> str:
        """确定 git 可执行文件：显式环境变量 > 配置 > PATH > 常见安装位置。

        便携测试可在任意平台用 SKILLS_SYNC_GIT 指定，不写死 Windows 路径。
        """
        for candidate in (
            os.environ.get("SKILLS_SYNC_GIT"),
            getattr(cfg, "git_executable", ""),
            "git",
        ):
            if not candidate:
                continue
            found = shutil.which(candidate)
            if found:
                return found
        for guess in (
            r"C:\Program Files\Git\cmd\git.exe",
            "/usr/bin/git",
            "/usr/local/bin/git",
            "/opt/homebrew/bin/git",
        ):
            if os.path.isfile(guess):
                return guess
        return "git"

    # ---------- 基础 ----------

    def _safe(self, text: str) -> str:
        return util.redact_text(text or "")

    def run(self, args: list[str], cwd: Path | None = None, check: bool = True,
            timeout: int | None = None, input_text: str | None = None) -> subprocess.CompletedProcess:
        cmd = [self.executable] + list(args)
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(cwd) if cwd else None,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout or self.timeout,
                env=self.env,
                input=input_text,
            )
        except FileNotFoundError as exc:
            raise GitError("未找到 git 可执行文件，请先安装 Git 并加入 PATH（或用 SKILLS_SYNC_GIT 指定路径）。") from exc
        except subprocess.TimeoutExpired as exc:
            raise GitError(
                f"git 命令超时（{timeout or self.timeout}s）：git {self._safe(' '.join(args))}",
                hint="检查网络或远端可达性；不完整操作可用 recover 恢复。",
            ) from exc
        if check and proc.returncode != 0:
            raise GitError(
                f"git {self._safe(' '.join(args))} 失败（退出码 {proc.returncode}）："
                f"{self._safe((proc.stderr or proc.stdout).strip())}",
                details={"args": [self._safe(a) for a in args], "returncode": proc.returncode},
            )
        return proc

    def version(self) -> str:
        return self.run(["--version"], check=False).stdout.strip()

    # ---------- 仓库状态 ----------

    def is_repo(self, hub: Path) -> bool:
        return (Path(hub) / ".git").exists()

    def dirty(self, hub: Path) -> list[str]:
        proc = self.run(["status", "--porcelain"], cwd=hub, check=False)
        return [line for line in proc.stdout.splitlines() if line.strip()]

    def head(self, hub: Path) -> str | None:
        proc = self.run(["rev-parse", "--verify", "HEAD"], cwd=hub, check=False)
        if proc.returncode != 0:
            return None
        return proc.stdout.strip()

    def branch_of(self, hub: Path) -> str:
        proc = self.run(["rev-parse", "--abbrev-ref", "HEAD"], cwd=hub, check=False)
        if proc.returncode != 0:
            return ""
        return proc.stdout.strip()

    def has_remote(self, hub: Path) -> bool:
        proc = self.run(["remote"], cwd=hub, check=False)
        return "origin" in proc.stdout.split()

    def remote_url(self, hub: Path) -> str:
        proc = self.run(["remote", "get-url", "origin"], cwd=hub, check=False)
        return proc.stdout.strip() if proc.returncode == 0 else ""

    def remote_head(self, hub: Path, branch: str) -> str | None:
        proc = self.run(["rev-parse", "--verify", f"origin/{branch}"], cwd=hub, check=False)
        if proc.returncode != 0:
            return None
        return proc.stdout.strip()

    # ---------- 初始化 / 远端 ----------

    def init_repo(self, hub: Path, branch: str) -> None:
        hub.mkdir(parents=True, exist_ok=True)
        self.run(["init", "-b", branch], cwd=hub)
        self._configure_repo(hub)

    def clone(self, url: str, hub: Path, branch: str) -> None:
        hub.parent.mkdir(parents=True, exist_ok=True)
        proc = self.run(["clone", "--branch", branch, url, str(hub)], check=False)
        if proc.returncode != 0:
            # 空仓库 + 指定 branch 时 clone 可能失败；退化为 init + remote add
            if hub.exists():
                from . import util

                util.remove_dir(hub)
            self.init_repo(hub, branch)
            self.run(["remote", "add", "origin", url], cwd=hub)
            return
        self._configure_repo(hub)

    def _configure_repo(self, hub: Path) -> None:
        # 保证字节级一致：禁用换行转换与安全换行检查。
        self.run(["config", "core.autocrlf", "false"], cwd=hub)
        self.run(["config", "core.safecrlf", "false"], cwd=hub)
        self.run(["config", "core.quotepath", "false"], cwd=hub)

    def set_remote(self, hub: Path, url: str) -> None:
        if self.has_remote(hub):
            self.run(["remote", "set-url", "origin", url], cwd=hub)
        else:
            self.run(["remote", "add", "origin", url], cwd=hub)

    def fetch(self, hub: Path) -> str:
        """拉取 origin；网络失败抛 GitError。返回本地记录的远端头（可能为空）。"""
        proc = self.run(["fetch", "--prune", "origin"], cwd=hub, check=False)
        if proc.returncode != 0:
            raise GitError(
                f"获取远端失败：{self._safe((proc.stderr or proc.stdout).strip())}",
                hint="检查网络与仓库访问权限；本机改动未被触碰，恢复网络后重试即可。",
            )
        return self.remote_head(hub, self.cfg.branch) or ""

    def reset_to_remote(self, hub: Path, backup_tag: str | None = None) -> None:
        """丢弃本地未推送提交，回到 origin/<branch>；先建备份引用防止历史丢失。"""
        head = self.head(hub)
        remote = self.remote_head(hub, self.cfg.branch)
        if head and backup_tag:
            self.run(["branch", "-f", backup_tag, head], cwd=hub, check=False)
        if remote:
            # checkout -B 让本地分支重新指向远端提交，避免 detached HEAD
            self.run(["checkout", "-B", self.cfg.branch, f"origin/{self.cfg.branch}"], cwd=hub)
            self.run(["reset", "--hard", f"origin/{self.cfg.branch}"], cwd=hub)
            self.run(["clean", "-fd"], cwd=hub, check=False)
        else:
            # 远端还没有分支：保留本地，等待首次 push
            pass

    def commit_all(self, hub: Path, message: str) -> str | None:
        dirty = self.dirty(hub)
        if not dirty:
            return None
        self.run(["add", "-A"], cwd=hub)
        self.run([
            "-c", f"user.name={self.cfg.git_author_name}",
            "-c", f"user.email={self.cfg.git_author_email}",
            "-c", "commit.gpgsign=false",
            "commit", "-m", message,
        ], cwd=hub)
        return self.head(hub)

    def is_ancestor(self, hub: Path, ancestor: str, descendant: str) -> bool:
        """ancestor 是否为 descendant 的祖先（用于判断本地是否领先远端）。"""
        proc = self.run(["merge-base", "--is-ancestor", ancestor, descendant], cwd=hub, check=False)
        return proc.returncode == 0

    def push(self, hub: Path) -> None:
        proc = self.run(["push", "origin", f"HEAD:{self.cfg.branch}"], cwd=hub, check=False)
        if proc.returncode != 0:
            stderr = self._safe((proc.stderr or proc.stdout).strip())
            raise GitError(
                f"推送被拒绝或失败：{stderr}",
                hint="可能是另一台设备先推送（会自动重算并重试）；持续失败请检查网络与权限。",
                details={"stderr": stderr},
            )

    def ls_remote(self, url: str) -> tuple[bool, str]:
        proc = self.run(["ls-remote", "--heads", url], check=False)
        if proc.returncode != 0:
            return False, self._safe((proc.stderr or proc.stdout).strip())
        return True, proc.stdout.strip()
