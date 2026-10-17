---
name: skills-sync
description: 在两台设备之间双向同步本机 Codex skill（完整文件快照，GitHub 私有仓库中转）。当用户说「同步 skill」「skill 同步」「把 skill 带到另一台机器」「检查 skill 有没有分叉」「卸载这个 skill 但别影响另一台」时使用。
disable-model-invocation: true
---

# skills-sync

手动触发的入口。底层是 `python -m skills_sync`（先按项目 README 安装程序，并设置 `PYTHONPATH=<程序目录>/src`）。
**不要凭记忆直接改 skill 目录或复制文件**——先看清计划，再执行，冲突时交给用户决定。

## 先确认安装

```powershell
python -m skills_sync doctor
```

- 退出码 0：环境就绪。
- 非 0：把输出原样告诉用户，不要自行 `git init`、改配置或删除状态目录。

下文用 `skills-sync` 代指 `python -m skills_sync`（保持 `PYTHONPATH=<安装目录>/src`）。

## 场景与动作

### 1. 用户说「同步一下」/「看看有没有要同步的」

```powershell
skills-sync plan          # 默认先获取远端，再算计划（只写本机共享库镜像，不写 skill 目录）
skills-sync plan --offline # 明确离线：只用上次已知的快照，输出会标 stale
```

`plan` 的输出里，先看这四个字段，其余都是细节：

- `remote_checked`：是否真的获取到了远端最新状态；为 `false` 时这份计划不算"最新"；
- `stale`：为 `true` 表示只基于上次已知的共享库快照，**不能**说"已是最新"；
- `needs_action`：需要用户决定的 skill id 数组（另有特殊值 `remote-check`）；
- `items[].action`：每个 skill 要做的动作。

`summary` 只是按 action 计数（`summary.total` 是**全部**条目数，包括 `unchanged`），
**不能用 `summary.total == 0` 判断"没事可做"**；判断依据是 `items[].action` 与 `needs_action`。

`items[].action` 的实际取值与处理方式：

| `action` | 含义 | 该怎么处理 |
| --- | --- | --- |
| `deploy` | 只有共享库改了，要更新本机 | 直接 `sync`（这是最常见的同步场景，不需要先做别的） |
| `push` | 只有本机改了，要推送到共享库 | 直接 `sync` |
| `push_new` | 已批准的新 skill，要纳入共享库 | 直接 `sync` |
| `merge` | 两端改了不同位置，可自动合并 | 直接 `sync` |
| `adopt` | 两端内容一致，只补齐基线 | 直接 `sync` |
| `delete_local` | 别的设备已全局删除，本机副本未被改动 | 直接 `sync`（移除前已备份） |
| `mark_uninstalled` | 共享库有这个 skill，本机目录缺失 | 直接 `sync`（记为"本机不安装"，不会传播删除） |
| `unchanged` | 两端一致，没有动作 | 只汇报，不必调 `sync` |
| `skip_local_only` / `skip_uninstalled` | 用户标记为仅本机 / 本机已卸载 | 只汇报；要改回来用 `restore <id>` 或改偏好 |
| `pending_add` | 本机新增，等待批准 | **必须问用户**；批准后 `include <id>`（多 root 同名加 `--root <key>`）再 `sync` |
| `conflict` | 内容冲突，或不同 root 下同名但内容不同 | 走第 3 节，**不要**直接 `sync` |
| `error` | 大小写冲突、目标不可用等硬错误 | 按 `detail` 修正后重跑 `plan` |

所以"同步一下"的标准流程是：

1. `skills-sync plan`（默认先获取远端）。
2. 若 `needs_action` 里只有 `remote-check`：说明没确认到远端，如实告诉用户，可重试或加 `--offline` 看离线快照，**不要**说已同步完。
3. 若 `items` 里有 `pending_add` / `conflict` / `error`：先按上表处理（批准、解决冲突、修错）。
4. 其余动作（`deploy`/`push`/`push_new`/`merge`/`adopt`/`delete_local`/`mark_uninstalled`）都可以直接 `sync`，一次调用把两端对齐。

已经由本工具管理的内容（上表这些常规动作）**不需要逐条再向用户索取批准**；
只有 `pending_add`（把新 skill 推进共享库）和 `conflict`（决定保留哪一边）必须让用户拍板。

`plan` 的退出码：`0` 表示计划可执行；`3` 表示有需要用户决定的事
（`needs_action` 非空，或 `stale` / 未确认远端）；`2` 表示读取失败。

不要把 `pending_add` 当成"自动纳入"；未批准的新 skill 不会推送。

### 2. 执行同步

```powershell
skills-sync sync
```

- 退出码 0：汇报 `pushed` / `deployed` / `merged` 数量即可。
- 退出码 3 且 `stale` 非空：同步过程中有计划后改动被保护性跳过（没有覆盖本机），要如实说出这些 skill 并让用户再跑一次 `sync`。
- 退出码 3 且提示"共享库已提交但本机未部署"：远端已经推送成功，但本机在落地前又被改动，工具没有覆盖它、也没有写"已部署"基线。如实说明这个状态，让用户看过本机内容后再跑一次 `sync`；**不要**说同步失败要回滚远端，也不要手工改共享库。
- 退出码 2：网络、权限或写入失败。把 `error` 与 `failures` 原样转述，并提醒用户"本机内容未丢，已备份在 `state/backups/<op_id>/`"；**不要**重试前先 `git reset`、删状态目录或手工覆盖文件。
- 退出码 3（其余情形）：见第 3 节。

## 解释器与程序定位

入口 skill 只负责"按场景调用命令"，真正执行的是 `python -m skills_sync`：

- 本包不自带解释器。先确认用哪个 `python`：Windows 上 `python` 可能指向 Microsoft Store 的占位程序（运行无输出），改用 `py -3` 或完整路径（如 `C:\Python312\python.exe`）；macOS 用 `python3`。
- 安装目录：把 `src/` 放到固定位置（例如 `~/.local/share/skills-sync/src`），并始终带 `PYTHONPATH`。验证：`python -m skills_sync doctor` 退出码 0。
- Git 定位顺序：环境变量 `SKILLS_SYNC_GIT` → 配置里的 `git.executable` → `PATH` 上的 `git` → 常见安装位置。找不到时 `doctor` 会明确报错，不会静默失败。
- 入口 skill 的放置：把 `skill/skills-sync/` 整个目录复制到 Codex 的 skill 根（如 `~/.codex/skills/skills-sync/`），Codex 从 skill 根发现它；`disable-model-invocation: true` 表示只在用户明确要求时使用。

### 3. 冲突

冲突不会自动合并，也不会把 `<<<<<<<` 写进本机文件。冲突目录在：

```
<配置目录>/state/conflicts/<conflict_id>/{base,local,remote}/ + diff.txt + conflict.json
```

做法：

1. 读 `diff.txt` 和 `conflict.json` 的 `kind` / `detail`，向用户说明"两边分别改了什么"。
2. 让用户选择：
   - 保留本机：`skills-sync resolve <id> --take local`
   - 采用远端：`skills-sync resolve <id> --take remote`
   - 手工合并后：把合并结果放进一个目录，`skills-sync resolve <id> --dir <目录>`
   - 删除/修改冲突且用户接受删除：`skills-sync resolve <id> --take remote`
3. `skills-sync continue` 应用。
4. 若 `continue` 报"内容在冲突解决后发生变化"，说明批准已过期：重新走第 1 步，**不要**强行继续。

不要替用户决定保留哪一边；不要 `abort` 之后又手工复制文件。

### 4. 卸载（只影响本机）

```powershell
skills-sync uninstall <id> --yes
```

先向用户确认 id；卸载会进"不安装清单"，其他设备不会受影响，也不会被自动装回。
用户如果要"两边都删掉"，那是另一件事：

```powershell
skills-sync delete-shared <id> --yes
```

执行前必须明确告知"这会把 skill 从共享库删除并传播到其他设备"，并说明：
删除前本机会有一份备份，**`restore <id>` 是"从共享库恢复"，全局删除后共享库已没有内容，要用 `recover --restore <op_id>/<id>` 从本机备份取回**。

### 5. 恢复

```powershell
skills-sync restore <id>                     # 从共享库恢复（本机内容与共享库不同时默认拒绝覆盖）
skills-sync restore <id> --force             # 确认丢弃本机未同步改动时才用
skills-sync recover                          # 看未完成操作与备份
skills-sync recover --restore <op_id>/<id>   # 从本机备份恢复（卸载/删除/失败回滚）
```

`recover --restore` 失败时不会报成功：备份仍保留，按提示修正权限/占用后重跑同一条命令。

## 硬性约束

- 不要修改 `<配置目录>/state/` 下的文件，不要手工编辑 `hub/`。
- 不要在未获批准时把新 skill 推入共享库。
- 不要用 `git push --force`、`git reset --hard` 处理同步失败；用 `skills-sync sync` 重试。
- `plan` 与 `compat` 是只读的，可以随时跑。
- 跨平台兼容提示只是提示：不要自动改写用户的脚本。
- 任何"部分完成"都要如实汇报，不要总结成"同步成功"。
