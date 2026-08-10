# Codex support record

核验日期：2026-08-09

详细任务、验收门槛与执行日志见 [实施计划](../../docs/IMPLEMENTATION_PLAN.md)。当前代码版本为 `0.3.0` 本机 Full-write Beta。M0.1–M4 自动 gate 已完成；M5/M6 自动核心与三类可信父任务裁决 receipt 已完成，Setup 已在当前工作区创建并校验 5 个项目 Agent。当前停在插件 App 安装、新任务发现和真实宿主/live gate，不进入 M7。

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
- hooks：协议、单测与 fail-closed 证据校验器已完成；未信任与 `/hooks` 持久化信任后的两次新任务 smoke 尚待用户操作，但只作为可选安全增强。`--dangerously-bypass-hook-trust` 不计作信任证据。
- 测试宿主：隔离 home、禁网、超时、状态双层保护和契约拆分已完成；全量测试须使用 `scripts/run_tests.ps1`。
- runtime：项目定位、Codex pointer/registry、参考资料 provenance 和旧 Claude 只读兼容已统一；M4 提供 Doctor 只读 SQLite、Query request-file/provenance 与 Dashboard 项目级安全生命周期；M5/M6 新增受控 Init/Learn/Plan/Review、严格 rollout/用户选择 receipt、事务 Write 与安全 Backup 真源校验。
- Skills：9/9 个 Skill 源适配已实现并通过静态合同。Learn、Review、Init、Plan 与 Write 自动核心已有 gate；Plan authored-conflict、Write blocking 定点修复逐 issue resolution 与作者正文/合同冲突恢复都使用可信父任务 receipt，裸字符串、过期/跨 scope/篡改证据保持 fail-closed。
- 验证：当前安全收集 1893 项；`full` 为 1771 passed、15 skipped、107 deselected，coverage 90.41%；9 个真实宿主保护路径零变化。真实安装后的新顶层任务发现、Apply/Agent/现场用户裁决和完整写章证据本轮未采集，当前不声称已发布。
- 自定义 Agents：五份规范合同与托管 TOML 生成器已完成。context/writer/reviewer/data 当前固定 `gpt-5.6-luna` / `high`，deconstruction 继承父任务配置；升级前的 Luna/medium 真实子任务轨迹保留为历史证据。合同更新后需重新执行 Setup Apply 并打开新任务。
- MCP/App：当前不需要。

## 降级原则

- 专用 Agent 缺失、不可发现或版本哈希不匹配时，阻断依赖该 Agent 的 Skill，并引导用户执行 `$webnovel-setup` 后打开新任务；禁止由主 Agent 冒充执行。
- hooks 未安装或未获信任时，核心流程仍须通过显式 `project-status`、`doctor` 和 `write-gate` 命令完成。
- 危险路径 hook 采用 fail-closed：shell 中显式引用受保护路径时，即使看起来是读取也会拒绝；只读诊断改走 runtime 命令。
- 模型或 Agent 不可用、实际模型不匹配、合同 hash 过期时阻断；TOML、canned fixture 或子 Agent 自报都不能替代真实 rollout 证据。
