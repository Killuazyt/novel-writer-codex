# Repository instructions

本仓库将 `webnovel-writer` 移植到 Codex。工作时遵守以下约束。

## New-task handoff（新对话必读）

### 权威上下文

- 唯一详细实施清单是 `docs/IMPLEMENTATION_PLAN.md`。每次开始实现前必须完整读取它，并以其中的复选框、前置 gate 和执行日志为准。
- `docs/PORTING.md` 只用于简要迁移状态，不得另建一份相互竞争的详细计划。
- 目标仓库：`F:\codexnovel\novel-writer-codex`。
- 本地上游参考源码：`F:\codexnovel\reference\webnovel-writer\webnovel-writer`。
- 上游仓库：`https://github.com/lingfengQAQ/webnovel-writer`；冻结基线为 `master@2041abad78211e29a67a2f0c64b2a97a747dce57`，上游版本标记 `6.2.1`。
- 目标 GitHub 仓库：`https://github.com/Killuazyt/novel-writer-codex.git`。

### 当前停点（2026-08-07）

- M0.1、M0、M1、M2 已完成并通过本地 gate；当前按范围停在 M2，下一项是 `M3`，未经用户明确要求不得提前创建生产 Skills 或 Agents。
- M2 已完成严格项目定位、Codex pointer/registry、参考资料 provenance、Codex package/version/release 校验和离线 upstream check/prepare；Story System 与小说数据合同未改。
- 当前安全收集 862 项；`full` 为 789 passed、73 deselected，`scripts/data_modules` coverage 90.19%，9 个真实宿主保护路径前后零变化。
- 已有 `upstream` remote 与 `upstream-lock.json`；仍未创建 9 个生产 Codex Skill、4 个项目 Agent、CI、Marketplace、tag 或 release。
- 全量 pytest 必须使用 `scripts/run_tests.ps1` 或显式 bootstrap 的定向命令；不得运行会绕过隔离、契约筛选或真实宿主状态保护的原始 pytest。
- 不要沿用聊天中的口头进度推断完成状态；先检查计划复选框、执行日志、工作树和实际测试证据。

### 已锁定的移植决策

- 这是仅维护 Codex 的下游；`.claude` 和 `CLAUDE_*` 只允许只读兼容 fallback，新流程不得写入 `.claude`。
- `.story-system`、`.webnovel`、`正文`、`设定集`、`大纲` 的业务契约保持兼容，不迁移小说数据。
- 最终交付 9 个 Codex Skill：保留 8 个 `$webnovel-*` 名称并新增 `$webnovel-setup`。
- 4 个专用 Agent 必须由 `$webnovel-setup` 显式安装为项目级 `.codex/agents/*.toml`。缺失或合同哈希过期时阻断相关 Skill，不允许主 Agent 静默模拟。
- `$webnovel-init` 默认 `--git-mode off`；初始化 Git、创建提交、push、tag、release 或任何外部发布都必须取得用户的单独明确授权。
- `$webnovel-review` 先实现单章版，1.0 前再完成一次最多 5 章、逐章串行且可恢复的范围审查。
- Windows 和中文路径是首要支持场景；Ubuntu 是正式辅助平台。所有文本读写显式使用 UTF-8，Skill/YAML/TOML 必须 UTF-8 无 BOM。
- 不自动安装依赖、打开浏览器、访问网络、初始化 Git、创建提交或修改父仓库。

### 后续逐项执行协议

1. 每个新对话先读取 `docs/IMPLEMENTATION_PLAN.md`，确认用户指定的唯一计划项及其前置 gate。
2. 检查本地权威源码、当前工作树和已有用户改动；一次只实现一个可独立验收的计划项，不擅自扩展到下一项。
3. 先运行该项的安全定向测试，再运行所属里程碑 gate；所有 pytest 均须保留 M1 建立的隔离、超时、契约筛选与真实状态保护。
4. 回读所有中文文件，检查 UTF-8、无 BOM、无乱码；同时检查 `git diff --check` 和与本项相关的安全边界。
5. 只在验证通过后更新对应复选框和实施日志，并汇报修改文件、验证命令、结果、限制及下一项。
6. 里程碑完成后先向用户汇报并等待确认；未经确认不创建提交。push、tag、release 永远另行授权。
7. 若官方 Codex 协议或上游 SHA 已变化，先记录 decision、更新 lock 与受影响测试，再继续实现，不得凭旧聊天内容假定当前协议。

## Source of truth

- `scripts/webnovel.py` 与 `scripts/data_modules/` 是业务逻辑真源；Codex adapter 不复制写章、提交、投影或记忆逻辑。
- `.story-system/commits/` 保存章节事实，`.webnovel/` 是投影/read model。修改这些数据必须走 runtime 命令。
- hooks 只做短状态提示与轻量守卫，不在 hook 中执行创作、安装依赖或长期服务。

## Codex adaptation

- Codex 插件清单位于 `.codex-plugin/plugin.json`。
- 仓库级指导放在 `AGENTS.md`；项目级自定义 Agent 放在 `.codex/agents/*.toml`。
- 可分发 Skill 位于 `skills/<skill-name>/SKILL.md`，只使用 Codex 已验证的字段和行为。
- 尚未适配的 Claude Code Skill/Agent 不得复制后直接宣称可用。
- 宿主差异记录在 `adapters/codex/`；每项支持声明都应有官方文档和 smoke 证据。

## Runtime safety

- 运行 Python 时使用 `python -X utf8`，文本读写显式指定 UTF-8。
- 支持 Windows PowerShell 与 POSIX shell；文档不要只给 Bash `export` 示例。
- 不直接写 `.story-system/commits/`、`.webnovel/index.db`、`.webnovel/vectors.db`、`.webnovel/memory_scratchpad.json` 或 `.webnovel/projection_log.jsonl`。
- `PreToolUse` 对 shell 中显式出现的受保护路径采用 fail-closed 策略，纯读取也会被挡；诊断和查询应优先走 `webnovel.py` 的只读命令。
- 章节提交使用 `webnovel.py chapter-commit`；失败投影使用 `webnovel.py projections retry/replay`。

## Validation

- 改 manifest/hook/adapter 后运行：`python -X utf8 scripts/validate_codex_adapter.py`。
- 改 hook 后运行：`python -X utf8 -m pytest scripts/tests/test_hooks.py -q -o addopts=''`（hook 测试通过子进程执行，不参与 runtime 覆盖率门槛）。
- 改 runtime 后先跑相关测试；只有 M1 隔离 gate 已完成，才可再跑 `python -X utf8 -m pytest -q`。
- 在 Windows 中文路径下做一次真实插件发现、`project-status` 与 `doctor` smoke，才可提升支持等级。

## Project hygiene

- 保留 GPL-3.0、上游作者与 `UPSTREAM.md` 中的基线信息。
- 不提交密钥、虚拟环境、缓存、测试生成的 `.story-system/` 或 `.webnovel/`。
- 保留无关的用户改动；不要用破坏性 Git 命令清理工作树。
