# Codex support record

核验日期：2026-08-07

详细任务、验收门槛与执行日志见 [实施计划](../../docs/IMPLEMENTATION_PLAN.md)。当前已完成 M0.1、M0、M1、M2，并按范围停在 M2；下一项是 M3。

## 已由官方文档确认

- 插件根使用 `.codex-plugin/plugin.json`，可包含 Skills 与 hooks。
- 仓库指导使用 `AGENTS.md`，Codex 会从仓库根到当前目录逐层读取。
- 仓库级 Skill 位于 `.agents/skills`；插件 Skill 位于插件 `skills/`。
- 项目级自定义 Agent 位于 `.codex/agents/*.toml`。
- hooks 支持 `SessionStart` 与 `PreToolUse`；插件命令获得 `PLUGIN_ROOT`，Windows 可用 `commandWindows`。

官方资料：

- [Build plugins](https://developers.openai.com/plugins/build/plugins)
- [AGENTS.md](https://learn.chatgpt.com/docs/agent-configuration/agents-md)
- [Skills](https://learn.chatgpt.com/docs/build-skills)
- [Subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents)
- [Hooks](https://learn.chatgpt.com/docs/hooks)

## 当前项目支持

- manifest：静态 scaffold 已完成。
- hooks：已适配协议并有本地单测；尚待真实安装/信任 smoke。
- 测试宿主：隔离 home、禁网、超时、状态双层保护和契约拆分已完成；全量测试须使用 `scripts/run_tests.ps1`。
- runtime：项目定位、Codex pointer/registry、参考资料 provenance 和旧 Claude 只读兼容已统一；离线 upstream drift/prepare 工具已通过冻结快照验证。
- Skills：尚未开放；将从 doctor/query 开始。
- 自定义 Agents：仅有映射设计，尚未生成项目级 TOML，也不宣称插件可自动分发这些 Agent。
- MCP/App：当前不需要。

## 降级原则

- 专用 Agent 缺失、不可发现或版本哈希不匹配时，阻断依赖该 Agent 的 Skill，并引导用户执行 `$webnovel-setup` 后打开新任务；禁止由主 Agent 冒充执行。
- hooks 未安装或未获信任时，核心流程仍须通过显式 `project-status`、`doctor` 和 `write-gate` 命令完成。
- 危险路径 hook 采用 fail-closed：shell 中显式引用受保护路径时，即使看起来是读取也会拒绝；只读诊断改走 runtime 命令。
