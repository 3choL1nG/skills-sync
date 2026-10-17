# 样例 skill（fixtures）

这个目录是一份可直接用于手工演示的第三方风格 skill 样例，自动化测试里也会构造等价内容。

结构：

```
sample-skill/
├── SKILL.md            # 指令
├── LICENSE             # 许可证（MIT）
├── scripts/run.py      # 脚本
└── templates/page.html # 模板
```

手工演示：

```powershell
$root = Join-Path $env:TEMP "skills-demo\codex"
New-Item -ItemType Directory -Force "$root\sample-skill" | Out-Null
Copy-Item -Recurse -Force "tests\fixtures\sample-skill\*" "$root\sample-skill\"
# 然后把 $root 作为 --root codex=... 传给 init，再 plan / include / sync
```

不要把这个目录直接放进真实的 `~/.codex/skills`；演示请用临时目录。
