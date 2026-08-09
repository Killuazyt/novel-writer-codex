# Codex 移植审计与路线

核验日期：2026-08-09
上游基线：`master@2041abad78211e29a67a2f0c64b2a97a747dce57`（manifest `6.2.1`）

详细任务、验收门槛和执行日志以 [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) 为唯一实施清单；本文只保留迁移状态摘要。

当前里程碑：当前代码版本为 `0.3.0` 本机 Full-write Beta。M0.1–M4 自动 gate 已完成；M5/M6 的自动核心与三类可信父任务裁决 receipt 已完成，5 个项目 Agent 已在当前工作区安装并回读为 current，个人 marketplace 已建立。剩余为插件点击安装、新任务发现和真实完整写作 live gate。M7/M8 的 CI、发布 Marketplace、多平台、外部安装和发布任务暂缓；本机 beta 不等于已创建 tag 或 GitHub Release。

## 1. 项目本质

上游不是一组简单提示词，而是一个完整的 Claude Code 插件：8 个业务 Skill、4 个专用 Agent、2 个 hook、统一 Python CLI、Story System、RAG/read model、Dashboard、模板和行为测试。真正值得复用的是 runtime 与数据合同；宿主层只是入口。

因此移植边界是：

```text
保留：runtime / Story System / schema / templates / references / dashboard
改写：manifest / skills / agents / hooks / 安装发布 / 宿主路径
验证：discover / doctor / query / write gates / commit / projection
```

## 2. 必须适配的问题

| 优先级 | 问题 | 原因与处理方向 |
|---|---|---|
| P0 | 插件清单与发布 | `.claude-plugin/plugin.json`、Claude marketplace 不能直接用于 Codex；使用 `.codex-plugin/plugin.json`，后续再决定个人/团队 marketplace |
| P0 | Skill 语法与调用 | 8 个 Skill 含 `allowed-tools`、`argument-hint`、`/webnovel-*`、Claude 工具名和 Bash 环境变量；逐项改成 Codex Skill 与 `$skill-name`/自然语言触发 |
| P0 | 写章 Agent 调度 | `webnovel-write` 硬编码 Claude Agent 名称；Codex 要区分项目级 `.codex/agents/*.toml` 与插件可分发的通用 subagent 提示 |
| P0 | 写章事务链 | 不能只验证“写出正文”；必须验证 prewrite、precommit、chapter-commit、projection、postcommit 与失败恢复 |
| P1 | hooks | Codex 支持相近事件，但变量、Windows 命令、拒绝 JSON 和 `apply_patch` 输入需要适配与测试 |
| P1 | 路径与全局配置 | `CLAUDE_PLUGIN_ROOT`、`CLAUDE_PROJECT_DIR`、`.claude`、`~/.claude` 需要通用解析或 Codex fallback |
| P1 | 校验器/eval/CI | 现有校验器、版本同步和行为断言绑定 Claude manifest、Agent 名和工具字段；需要 Codex 专用校验与 smoke |
| P2 | Dashboard 启动 | Codex Desktop/Windows 下应默认 `--no-browser`，后台进程使用隐藏窗口，并明确端口/日志 |
| P2 | 文档机械替换 | 大量 `/webnovel-*` 与 `${CLAUDE_PLUGIN_ROOT}` 不能盲目全局替换，应从活跃 Skill 调用链逐项迁移 |

## 3. 已完成基础与当前迁移状态

- 建立 Codex manifest、GPL-3.0 派生归属、上游 lock 与只读 remote。
- 锁定上游 330 个插件子树文件及逐文件哈希，建立 archive/secret/UTF-8/BOM/whitespace 仓库卫生检查。
- 统一 `WEBNOVEL_HOME`、native registry 与 `.env` 优先级；旧 Claude registry 和 `.env` 仅只读 fallback。
- 建立 pytest 导入前隔离、禁网、30 秒超时、双层真实用户目录保护及主契约分类。
- 提供 `smoke`、`collect`、`full`、`upstream-collect` runner；862 项安全收集，当前 Codex 有效全集 789 项通过，coverage 90.19%。
- preflight 与 Codex validator 返回结构化结果；hook 已保护 state、summary 及其他 runtime 投影。
- 项目定位固定为 CLI、环境、CWD、Codex pointer/registry、旧 pointer/registry 的顺序，并返回来源与兼容模式；显式路径必须直接是书项目根。
- 新绑定只写明确 workspace 的 `.codex/.webnovel-current-project` 与 `WEBNOVEL_HOME`；旧 `.claude` 状态只读，参考资料按 Codex 项目、旧项目、插件内置逐文件回退。
- package/version/release 校验已切换到 Codex 单根布局；离线 upstream check/prepare 已验证冻结源 330/330 与幂等暂存。
- M3 新增 `$webnovel-setup`、五份规范 Agent 合同、托管 TOML 生成/冲突/回滚/幂等、有限选项与严格 Agent runtime gate。
- 两个不同父模型的新 Desktop 任务共生成 8 份固定角色 rollout；显式证据解析确认全部为 `gpt-5.6-luna / medium`，父子 id 与角色均匹配。另一个父任务独立完成规划且工具调用为 0。
- 当前安全收集 1055 项；`full` 为 978 passed、2 skipped、75 deselected，coverage 90.50%。`/hooks` 未信任→持久化信任现场证据尚未采集，但已降为可选安全增强，不影响 M3 完成状态。

Setup、Doctor、Query、Dashboard 与项目 Agents 已实施；Learn、Review、Init、Plan、Write 的 Codex Skill 源和 runtime 入口也已适配。Plan authored-conflict、Write blocking 定点修复逐 issue resolution 与作者正文冲突恢复均使用可信父任务 scope-bound receipt，并有生产级对抗测试；但不能描述成完整 write/review live 链已验收：真实 Apply/Agent/用户选择/projection 故障/Git backup 证据仍待采集。模型不可用、身份/合同/hash 不匹配时禁止回退。

当前安全收集 1893 项；`full` 为 1771 passed、15 skipped、107 deselected，coverage 90.41%，9 个真实宿主保护路径零变化。9 个 Skill 源适配与三类父任务裁决均已通过 package/Skill/生产对抗合同，但静态及 fixture 通过不替代新任务发现或真实模型/用户选择证据。

## 4. 推荐迁移顺序

1. `webnovel-doctor`：只读，先证明插件发现、项目根、依赖和中文路径。
2. `webnovel-query`：只读，验证 Story System/RAG 查询和输出格式。
3. `webnovel-dashboard`：只读服务，补后台启动与日志语义。
4. `webnovel-review`：读取章节并产出审查，不修改事实数据。
5. `webnovel-init`、`webnovel-plan`、`webnovel-learn`：受控写入，补确认与回滚边界。
6. `webnovel-write`：最后迁移并做完整事务链验收。

## 5. 支持等级门槛

| 等级 | 必须通过 |
|---|---|
| Scaffold | manifest 静态校验、hook 单测、归属与能力文档 |
| Read-only alpha | Codex 实际发现；Windows 中文路径下 `project-status`、`doctor`、`query` smoke |
| Write beta | init/plan/review/learn 测试；危险直写守卫和失败恢复 |
| Full beta | 专用 Agent 安装、版本与哈希均通过；完整写章事务链与行为 eval |
| Stable | 全量回归、安装/升级/卸载、上游 drift、发布文档均通过 |

当前为 **0.3.0 本机 beta、M5/M6 自动核心完成、live gate 未关闭**：本地 runtime/Skill 合同与三类父任务裁决对抗 gate 已通过；Setup 已创建并校验 5 个项目 Agent。插件 App 安装、新顶层任务发现、Init/Review/Plan/Write 现场回答、完整四 Luna 写章链和 projection 故障恢复仍待采集。任何缺失的真实宿主证据继续 fail-closed，不应对外宣称完整写章链已现场验收。
