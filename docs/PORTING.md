# Codex 移植审计与路线

核验日期：2026-08-06
上游基线：`master@2041abad78211e29a67a2f0c64b2a97a747dce57`（manifest `6.2.1`）

详细任务、验收门槛和执行日志以 [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) 为唯一实施清单；本文只保留迁移状态摘要。

当前里程碑：M0.1、M0、M1 已于 2026-08-06 完成并验收；实施已按范围停在 M1，M2 及以后尚未开始。

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

## 3. 已完成的 M0/M1 基础

- 建立 Codex manifest、GPL-3.0 派生归属、上游 lock 与只读 remote。
- 锁定上游 330 个插件子树文件及逐文件哈希，建立 archive/secret/UTF-8/BOM/whitespace 仓库卫生检查。
- 统一 `WEBNOVEL_HOME`、native registry 与 `.env` 优先级；旧 Claude registry 和 `.env` 仅只读 fallback。
- 建立 pytest 导入前隔离、禁网、30 秒超时、双层真实用户目录保护及主契约分类。
- 提供 `smoke`、`collect`、`full`、`upstream-collect` runner；798 项安全收集，当前 Codex 有效全集 732 项通过。
- preflight 与 Codex validator 返回结构化结果；hook 已保护 state、summary 及其他 runtime 投影。

Skills、项目 Agents、工作区 pointer 全面 Codex 化和发布链仍未实施，分别留在 M2/M3 及后续里程碑。

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

当前仅为 **Scaffold**，不应对外宣称完整可用。
