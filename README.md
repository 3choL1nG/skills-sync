# skills-sync

通过自己的 **GitHub 私有仓库**，在 Mac 和 Windows 之间双向同步 Codex 用户 skills。

同步 `SKILL.md`、脚本、模板、图片和其他资源，也保留你对第三方 skill 的本地修改。两端都可以创建、安装和修改 skill；遇到分叉时根据共同基线合并，有冲突则保留证据，等你决定。

```text
Mac 的用户 skills  ⇄  私有 GitHub 数据仓库  ⇄  Windows 的用户 skills
```

本仓库提供**同步程序、Codex 入口 skill 和测试**。实际 skill 内容存到你另建的私有数据仓库；不要把个人 skills 或凭据提交到本项目。

## 功能

- 支持同时管理 `~/.codex/skills` 和 `~/.agents/skills`，可配置其他根目录。
- 先查看计划，再执行手动同步；新发现的本机 skill 需要批准后才上传。
- 根据共同基线做三方合并，不按修改时间判断覆盖顺序。
- 冲突保留 base/local/remote 快照，不把冲突标记写进正在使用的文件。
- 覆盖和删除前备份；计划或批准后文件再次被修改，会停止相关操作并保护新修改。
- 区分本机卸载与全局删除；本机卸载不会影响另一台设备，也不会被自动装回。
- 默认过滤系统目录、缓存、常见凭据文件和部分令牌模式；拒绝跟随符号链接及 Windows junction。
- 提供跨平台兼容提示、JSON 输出和明确退出码。

这是手动同步工具。它不会更新第三方 skill 的上游版本，也不会自动安装脚本依赖或改写平台专用命令。

## 环境

| 依赖 | 要求 |
| --- | --- |
| Python | 3.10+，仅使用标准库 |
| Git | 2.23+ |
| 认证 | Git 能访问你的私有 GitHub 数据仓库，使用已有 SSH 或 HTTPS 凭据 |

当前代码版本：`0.1.1+codex`。Windows 回归、真实 GitHub 往返及 Mac 端推送到 Windows 的部署已有验证；完整 macOS 回归结果仍待补充，见[验证记录](docs/验证记录.md)。

## 1. 安装程序

### Windows / PowerShell

```powershell
git clone https://github.com/3choL1nG/skills-sync.git
cd skills-sync
$env:PYTHONPATH = (Join-Path $PWD 'src')
python -m skills_sync --version
```

### macOS / Terminal

```bash
git clone https://github.com/3choL1nG/skills-sync.git
cd skills-sync
export PYTHONPATH="$PWD/src"
python3 -m skills_sync --version
```

以下示例使用 Windows 的 `python`；Mac 对应换成 `python3`。每次打开新终端时，需要重新设置 `PYTHONPATH` 为本项目 `src` 的绝对路径。也可以直接运行 `python /你的项目路径/run.py <命令>`，该方式无需设置 `PYTHONPATH`。

Windows 上如果 `python` 指向 Microsoft Store 占位程序，请换成实际的 Python 解释器，例如 `py -3` 或解释器完整路径。

## 2. 连接自己的私有数据仓库

在 GitHub 新建一个**空的私有仓库**，例如 `codex-skills-sync`，不预置 README、许可证或 `.gitignore`。下面用 HTTPS 地址示范；将 `YOUR_ACCOUNT` 替换成你的账号，也可使用 SSH 地址。

Windows：

```powershell
python -m skills_sync init `
  --remote https://github.com/YOUR_ACCOUNT/codex-skills-sync.git `
  --root "codex=$HOME\.codex\skills" `
  --root "agents=$HOME\.agents\skills" `
  --default-root codex
```

Mac：

```bash
python3 -m skills_sync init \
  --remote https://github.com/YOUR_ACCOUNT/codex-skills-sync.git \
  --root "codex=$HOME/.codex/skills" \
  --root "agents=$HOME/.agents/skills" \
  --default-root codex
```

配置默认保存在 `~/.skills-sync/config.json`；共享库镜像、备份和本机状态位于 `~/.skills-sync/` 下。两台设备各自初始化，使用同一个私有仓库地址，分别保留自己的配置和状态。只使用一个 skill 根目录时，省略另一个 `--root`。

不要将 token 放进仓库 URL。使用 Git 的凭据管理器、SSH key 或事先完成 GitHub 登录。

## 3. 首次同步

```powershell
python -m skills_sync doctor
python -m skills_sync plan
```

阅读计划后，明确批准要上传的新 skill：

```powershell
python -m skills_sync include my-skill
# 确认全部纳入时才使用：
python -m skills_sync include --all-pending
python -m skills_sync sync
python -m skills_sync plan
```

另一台设备按第 1、2 步安装并连接同一数据仓库，再执行 `plan`。远端已有、本机没有的 skill 会显示为 `deploy`；没有冲突或待批准项时执行 `sync` 即可安装。如果本机已有同名但不同内容的 skill，会进入冲突流程，不能直接覆盖。

同名 skill 位于两个根目录时，用 `include my-skill --root codex` 指定参与同步的副本。批准绑定该副本及当时的内容版本；其他同名副本保持原样，批准后再修改内容需要重新批准。

## 4. 日常使用

```powershell
python -m skills_sync plan
python -m skills_sync sync
```

`sync` 是双向操作，可能同时上传本机改动和部署远端改动，先看计划。没有修改时不会创建空提交。

| 计划动作 | 处理 |
| --- | --- |
| `deploy` | 远端新增或更新，执行 `sync` |
| `push` / `push_new` | 本机修改或已批准的新增，执行 `sync` |
| `merge` / `adopt` | 自动合并或补齐基线，执行 `sync` |
| `pending_add` | 先决定是否纳入，再 `include` |
| `conflict` | 阅读冲突快照并选择解决方案 |
| `delete_local` | 远端全局删除，本机未修改；`sync` 备份后移除 |
| `mark_uninstalled` | 本机目录已移除；`sync` 记录为本机卸载 |
| `unchanged` | 无需操作 |
| `skip_local_only` / `skip_uninstalled` | 按本机偏好跳过 |
| `error` | 按提示处理后重新查看计划 |

`plan` 默认先检查远端；`plan --offline` 只查看缓存，会标记为未确认最新。JSON 输出时看 `remote_checked`、`stale`、`needs_action` 和各项 `action`；`summary.total` 包含未变化的条目，不能据此判断是否需要同步。

## 在 Codex 中使用

程序安装好后，可将本仓库的 [`skill/skills-sync`](skill/skills-sync/SKILL.md) 目录放入你的用户 skill 根目录。已有同名入口时，先比较版本，避免直接覆盖。

入口使用 `disable-model-invocation: true`，供用户明确调用。向 Codex 说明程序目录和本机 Python 调用方式，然后提出：

> 使用 skills-sync 查看同步计划，把另一台机器的更新同步过来。

入口会先检查环境和计划；常规已管理内容可直接同步，新增纳入和冲突取舍由用户决定。仅复制 `SKILL.md` 不会安装底层 Python 程序。

## 冲突处理

冲突时，本机文件保持原样。快照及差异位于：

```text
~/.skills-sync/state/conflicts/<conflict_id>/
  base/
  local/
  remote/
  diff.txt
  conflict.json
```

阅读差异后，选择其中一种方式：

```powershell
python -m skills_sync resolve my-skill --take local
# 或采用远端：resolve my-skill --take remote
# 或指定手工合并结果：resolve my-skill --dir <合并目录>
python -m skills_sync continue
```

解决方案会冻结为快照。批准后任一输入发生变化，`continue` 会要求重新确认。首次导入差异、文本重叠、二进制双端修改、删除与修改并存、同名多根分叉均按冲突处理。

## 卸载、删除和恢复

| 命令 | 作用 |
| --- | --- |
| `uninstall my-skill --yes` | 仅卸载本机，另一台设备不受影响 |
| `restore my-skill` | 从共享库恢复；默认拒绝覆盖本机未同步改动 |
| `delete-shared my-skill --yes` | 从共享库删除并传播到其他设备 |
| `recover` | 查看未完成操作和备份 |
| `recover --restore <op_id>/<skill_id>` | 从本机备份恢复 |
| `include my-skill --exclude` | 标记为仅本机，不上传 |

命令前统一加 `python -m skills_sync`。全局删除后共享库已无内容，需使用本机备份恢复，不能用 `restore` 从已删除的共享库取回。`restore --force` 会允许覆盖本机不同内容，只在明确丢弃这些修改时使用。

## 失败与排查

- 用 `doctor` 检查 Python、Git、配置和远端访问，用 `status` 查看本机状态。
- 网络恢复后正常重试 `sync`；不要用强推、手工改状态文件或删除备份来处理失败。
- 大量资源的首次上传可能触发默认 120 秒超时，可将本机配置的 `git.timeout_seconds` 调大，例如 `600`。超时不等于远端一定没收到提交，应重新检查远端后正常重试。
- 如果提示“共享库已提交但本机未部署”，远端已有内容，本机新编辑被保留。检查本机修改后再同步，不要强制覆盖或回滚远端。
- 跨平台提示可用 `compat` 查看。提示不代表脚本一定不可用，也不代表依赖已经安装。
- 凭据过滤只覆盖部分常见规则。上传前检查纳入范围；数据仓库应保持私有。

| 退出码 | 含义 |
| --- | --- |
| `0` | 成功 |
| `1` | 参数或配置错误 |
| `2` | 执行失败，检查具体错误和备份记录 |
| `3` | 待批准、冲突、内容变化或远端未确认，需要处理 |

子命令可加 `--json` 获取结构化输出，`--help` 查看完整参数。

## 开发与验证

```powershell
$env:PYTHONPATH = (Join-Path $PWD 'src')
python -m unittest discover -s tests -v
```

Mac 使用 `export PYTHONPATH="$PWD/src"` 和 `python3`。集成测试在临时目录启动本地 Git smart-HTTP 服务，模拟两台设备；不会访问你的真实同步数据。部分 Windows 环境没有创建符号链接权限，相应测试会跳过。

```text
src/skills_sync/       同步程序（仅标准库）
skill/skills-sync/     Codex 入口指令
tests/                集成测试与回归测试
docs/                 验证说明
run.py                无需 PYTHONPATH 的命令入口
```
