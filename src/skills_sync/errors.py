"""skill-sync 的错误类型与退出码约定。"""

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_FAILED = 2
EXIT_NEEDS_ACTION = 3

STATUS_OK = "ok"
STATUS_NEEDS_ACTION = "needs_action"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"


class SyncError(Exception):
    """可预期的操作失败：会被 CLI 转成结构化失败输出。"""

    exit_code = EXIT_FAILED
    status = STATUS_FAILED

    def __init__(self, message, *, hint=None, details=None):
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.details = details or {}


class UsageError(SyncError):
    exit_code = EXIT_USAGE
    status = STATUS_FAILED


class StateError(SyncError):
    """状态文件损坏、计划失效或前置条件不满足。"""


class GitError(SyncError):
    """git 子命令失败（含网络失败、推送被拒）。"""


class ConflictPending(SyncError):
    """存在待用户处理的冲突/审批，同步被有意暂停。"""

    exit_code = EXIT_NEEDS_ACTION
    status = STATUS_NEEDS_ACTION


class DeployError(SyncError):
    """安装目录写入失败；调用方须先回滚并记录。"""
