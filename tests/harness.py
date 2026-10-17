"""测试脚手架：临时目录 + 本地 smart-HTTP git 远端 + CLI 调用封装。

注：本机 DSH 沙箱内 MSYS sh.exe 不可用（无法创建 signal pipe），
因此 git 的 file:// / git:// 传输都不可用；这里用 git http-backend
起一个仅监听 127.0.0.1 的 smart-HTTP 服务模拟 GitHub 远端。
"""
from __future__ import annotations

import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

import shutil

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
SRC = REPO_ROOT / "src"


def _find_git() -> str:
    """优先 SKILLS_SYNC_GIT，其次 PATH 上的 git，最后才是常见安装位置。"""
    explicit = os.environ.get("SKILLS_SYNC_GIT", "").strip()
    if explicit:
        return explicit
    found = shutil.which("git")
    if found:
        return found
    for candidate in (
        r"C:\Program Files\Git\cmd\git.exe",
        r"C:\Program Files (x86)\Git\cmd\git.exe",
        "/usr/bin/git",
        "/usr/local/bin/git",
        "/opt/homebrew/bin/git",
    ):
        if Path(candidate).is_file():
            return candidate
    return "git"


GIT = _find_git()
PY = sys.executable

_server_cfg: dict = {}


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _run(self, method: str) -> None:
        body = b""
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            body = self.rfile.read(length)
        env = dict(os.environ)
        env.update({
            "GIT_PROJECT_ROOT": _server_cfg["root"],
            "GIT_HTTP_EXPORT_ALL": "1",
            "REQUEST_METHOD": method,
            "PATH_INFO": self.path.split("?")[0],
            "QUERY_STRING": self.path.split("?", 1)[1] if "?" in self.path else "",
            "CONTENT_TYPE": self.headers.get("Content-Type", ""),
            "CONTENT_LENGTH": str(length),
            "REMOTE_ADDR": "127.0.0.1",
        })
        proc = subprocess.run([GIT, "http-backend"], input=body, env=env,
                              capture_output=True)
        head, _, payload = proc.stdout.partition(b"\r\n\r\n")
        status = "200 OK"
        headers = []
        for line in head.split(b"\r\n"):
            if not line:
                continue
            name, _, value = line.decode("latin-1").partition(":")
            if name.lower() == "status":
                status = value.strip()
            else:
                headers.append((name.strip(), value.strip()))
        code = int(status.split()[0])
        self.send_response(code, status.split(" ", 1)[1] if " " in status else "OK")
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):  # noqa: N802
        self._run("GET")

    def do_POST(self):  # noqa: N802
        self._run("POST")


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):  # 客户端中断连接属正常现象，不打印堆栈
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def start_server(root: Path) -> int:
    """在后台线程启动 smart-HTTP 服务，返回端口。"""
    _server_cfg["root"] = str(root)
    port = _free_port()
    srv = _Server(("127.0.0.1", port), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _server_cfg["server"] = srv
    return port


def git(*args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    proc = subprocess.run([GIT, *args], cwd=str(cwd) if cwd else None, env=env,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} 失败：{proc.stderr}")
    return proc


class Device:
    def __init__(self, world: "World", label: str, roots: dict[str, Path],
                 remote_url: str, default_root: str = "codex", name: str | None = None):
        self.world = world
        self.label = label
        self.base = world.base / label
        self.base.mkdir(parents=True, exist_ok=True)
        self.config = self.base / "config.json"
        self.roots = roots
        for path in roots.values():
            path.mkdir(parents=True, exist_ok=True)
        args = ["init", "--remote", remote_url, "--name", name or f"dev-{label}"]
        for key, path in roots.items():
            args += ["--root", f"{key}={path}"]
        args += ["--default-root", default_root]
        rc, data = self.run(*args)
        if rc != 0:
            raise AssertionError(f"{label} init 失败：{json.dumps(data, ensure_ascii=False)}")
        self.init = data

    def run(self, *args: str, extra_env: dict | None = None,
            timeout: int = 120) -> tuple[int, dict]:
        env = dict(os.environ)
        env.update({
            "PYTHONPATH": str(SRC),
            "PYTHONIOENCODING": "utf-8",
            "GIT_TERMINAL_PROMPT": "0",
            "SKILLS_SYNC_CONFIG": "",
        })
        env.pop("SKILLS_SYNC_CONFIG", None)
        if extra_env:
            env.update(extra_env)
        cmd = [PY, "-m", "skills_sync", "--json", "--config", str(self.config), *args]
        proc = subprocess.run(cmd, cwd=str(REPO_ROOT), env=env, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=timeout)
        out = (proc.stdout or "").strip()
        try:
            data = json.loads(out) if out else {}
        except json.JSONDecodeError:
            data = {"_raw": out, "_stderr": proc.stderr}
        return proc.returncode, data

    def write_skill(self, root_key: str, skill_id: str, files: dict[str, str], *,
                    binary: dict[str, bytes] | None = None) -> Path:
        target = self.roots[root_key] / skill_id
        target.mkdir(parents=True, exist_ok=True)
        for rel, text in files.items():
            path = target / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        for rel, blob in (binary or {}).items():
            path = target / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(blob)
        return target

    def read(self, root_key: str, skill_id: str, rel: str) -> str:
        return (self.roots[root_key] / skill_id / rel).read_text(encoding="utf-8")

    def state(self) -> dict:
        return json.loads((self.base / "state" / "state.json").read_text(encoding="utf-8"))


class World:
    def __init__(self, name: str = "skills-sync-test"):
        tmp_root = REPO_ROOT / ".tmp-tests"
        tmp_root.mkdir(parents=True, exist_ok=True)
        # 不用 tempfile.mkdtemp：本机沙箱下 mkdtemp 创建的目录无法再建子目录
        self.base = tmp_root / f"{name}-{uuid.uuid4().hex[:10]}"
        self.base.mkdir(parents=True)
        self.remote_root = self.base / "remotes"
        self.remote_root.mkdir(parents=True)
        self.port = start_server(self.remote_root)
        self.remote_url = f"http://127.0.0.1:{self.port}/remote.git"
        self.make_remote()

    def make_remote(self) -> Path:
        bare = self.remote_root / "remote.git"
        if bare.exists():
            shutil.rmtree(bare)
        git("init", "--bare", "-q", "-b", "main", str(bare))
        git("-C", str(bare), "config", "http.receivepack", "true")
        git("-C", str(bare), "config", "http.uploadpack", "true")
        return bare

    def device(self, label: str, roots: dict[str, Path], **kwargs) -> Device:
        return Device(self, label, roots, self.remote_url, **kwargs)

    def cleanup(self) -> None:
        srv = _server_cfg.get("server")
        if srv is not None:
            srv.shutdown()
            srv.server_close()
            _server_cfg.pop("server", None)
        shutil.rmtree(self.base, ignore_errors=True)


class SyncTestCase(unittest.TestCase):
    """每个测试独立 World；子类可用 make_world 定制。"""

    def setUp(self) -> None:
        self.world = World(self.__class__.__name__)

    def tearDown(self) -> None:
        self.world.cleanup()

    def two_devices(self, a_roots: dict[str, str] | None = None,
                    b_roots: dict[str, str] | None = None) -> tuple[Device, Device]:
        a_roots = a_roots or {"codex": "codex", "agents": "agents"}
        b_roots = b_roots or {"codex": "codex"}
        a = self.world.device("A", {k: self.world.base / "A" / "roots" / v for k, v in a_roots.items()})
        b = self.world.device("B", {k: self.world.base / "B" / "roots" / v for k, v in b_roots.items()})
        return a, b

    def ok(self, device: Device, *args: str, **kwargs):
        rc, data = device.run(*args, **kwargs)
        if rc != 0:
            raise AssertionError(f"{device.label} {' '.join(args)} 返回 {rc}："
                                 + json.dumps(data, ensure_ascii=False, indent=2))
        return data

    def plan(self, device: "Device") -> dict:
        """plan 在有待批准/冲突时返回 3，这里统一按“可读计划”处理。"""
        rc, data = device.run("plan")
        if rc not in (0, 3):
            raise AssertionError(f"{device.label} plan 返回 {rc}："
                                 + json.dumps(data, ensure_ascii=False, indent=2))
        return data

    def allow_all(self, device: Device) -> None:
        self.ok(device, "include", "--all-pending")

    def skill_md(self, title: str, body: str = "") -> dict[str, str]:
        return {"SKILL.md": f"# {title}\n\n{body or title + ' skill.'}\n"}
