# Claude Code → Codex 工具映射

| 上游表达 | Codex 适配 | 备注 |
|---|---|---|
| `/webnovel-*` | `$webnovel-*` 或自然语言触发 Skill | 不在文档中假设 slash command |
| `Read` | 文件读取/搜索工具 | 优先 `rg` 定位，再读取必要范围 |
| `Grep` / `Glob` | `rg` / `rg --files` | Windows 与 POSIX 均可用时优先 |
| `Bash` | shell 工具 | 命令示例分别给 PowerShell/POSIX 或调用 Python CLI |
| `Write` / `Edit` | `apply_patch` | hooks 必须检查 patch header 中的目标路径 |
| `Agent` | Codex subagent | 专用角色是否可分发需单独验证；不能只复制 Claude Agent Markdown |
| `AskUserQuestion` | 主对话有限选项 | 优先客户端结构化选择；不可用时给出等价编号选项并等待回答，不采用默认分支 |
| `WebSearch` / `WebFetch` | Web 工具 | 外部事实按需核验并引用来源 |
| `${CLAUDE_PLUGIN_ROOT}` | `${PLUGIN_ROOT}`（hook）/ 脚本自定位 | runtime 兼容旧变量但新 Codex 文档不用旧变量 |
| `${CLAUDE_PROJECT_DIR}` | 显式 `--project-root` 或当前工作区 | 关键命令必须明确项目根 |

权限边界不从 Claude 的 `allowed-tools` 自动继承；Codex Skill 正文和 runtime/hook 需要共同约束。
