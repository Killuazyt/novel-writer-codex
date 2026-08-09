# Codex adapter smoke

核验日期：2026-08-09

## M3 自动 gate

```powershell
python -X utf8 scripts/validate_codex_adapter.py --format json
python -X utf8 scripts/validate_plugin_package.py --strict --format json
python -X utf8 scripts/validate_repository_hygiene.py --format json
python -X utf8 scripts/webnovel.py codex-setup --workspace-root "<工作区>" --check --format json
powershell -NoProfile -File scripts/run_tests.ps1 -Mode collect
powershell -NoProfile -File scripts/run_tests.ps1 -Mode upstream-collect
powershell -NoProfile -File scripts/run_tests.ps1 -Mode full
```

Setup 的前向检查已在包含中文、空格、括号和 `&` 的 Windows 路径完成：`--check` 零写入，确认后的 `--apply` 生成五个 TOML，再次检查返回 `current`。当前真实工作区也已完成同一 Apply/check 流程；仓库证据不记录用户机器绝对路径。

## 真实模型路由

Codex Desktop 中分别以 `gpt-5.6-sol / high` 和 `gpt-5.6-terra / high` 启动两个全新父任务。每个父任务依次实际调用 context、writer、reviewer、data；显式 rollout 解析器逐份绑定 child thread、双重 parent id、role、model 与 effort。结果为 8/8 子任务均使用 `gpt-5.6-luna / medium`，无父模型 fallback。

另一个 `gpt-5.6-sol / high` 新任务只由父任务生成三步规划；其 rollout 中工具调用数为 0，没有启动 writer/reviewer。结构化、脱敏的角色引用与原始 rollout SHA-256 见 [M3 模型路由证据](evidence/m3-model-routing-2026-08-07.json)。真实任务 UUID、原始 session 文件、提示词和生成正文不进入仓库。

本机 WindowsApps 中的 `codex.exe` 探针返回 `codex_cli_access_denied`（WinError 5），所以 CLI 探针明确标为 blocked，未被当作模型证据；上面的模型证据来自 Desktop 创建的新任务及其原始 rollout。

## Hook 持久化信任：可选安全增强

```powershell
python -X utf8 scripts/codex_m3_smoke.py hook-plan --hooks-config hooks/hooks.json --workspace-root "<工作区>"
```

当前 hook 配置 SHA-256 为 `30c240e35c646ca81351be250d3bb0ea47ee08d930b05c2bb11e4f8f65ab0187`，校验器按设计返回 `live_hook_evidence_required`。该现场证据不再是 M3 或后续里程碑 blocker；如用户选择补做，则按以下步骤采集：

1. 在未信任该 hash 的新任务中确认 hook 被跳过，同时 runtime gate 与保护快照仍安全。
2. 由用户在 `/hooks` 审核中持久化信任这个准确 hash。
3. 再开一个新任务，确认 hook 触发并拒绝受保护变更，同时 runtime gate 与保护快照仍安全。
4. 将两阶段证据交给 `verify-hook`；`--dangerously-bypass-hook-trust` 永远不算通过。

无论是否补做，runtime gate、受保护路径校验、Agent 合同 hash 和 schema 校验都必须通过自动测试；Hook 只提供纵深防御，不能替代这些硬边界。

## M4 Read-only Alpha

三个 M4 Skill 的结构验证、adapter/package/hygiene 校验与隔离 runner 已通过。`collect` 为 1132 项；`full` 为 1042 passed、3 skipped、87 deselected，`scripts/data_modules` coverage 90.34%，9 个宿主保护路径零变化。

`python -X utf8 scripts/codex_m4_smoke.py` 在隔离 `WEBNOVEL_HOME` 与含中文、空格、括号、`&` 的临时小说路径真实启动 Dashboard：动态端口 start/status 成功，`/api/project/info` 与 `/api/story-runtime/health` 均为 200，路径穿越为 403，stop 后为 `not_running`，小说事实前后 hash 不变。Query 另以真实 PowerShell command string 验证恶意实体名/domain 只进入 schema 化 request file，sentinel 未生成。

本轮明确禁止创建 Codex 顶层任务，因此没有采集“真实安装后的新任务发现三个 Skill”证据；静态 validator、当前任务内子 Agent 或文件存在性均不替代该发布 smoke。M4 源码实现完成，但 0.1.0 未发布。

## M5/M6 自动 gate

9 个 Skill 源适配已经落地。2026-08-09 最新隔离结果：`collect` 为 1893 项；`full` 为 1771 passed、15 skipped、107 deselected，`scripts/data_modules` coverage 90.41%，9 个宿主保护路径零变化；`upstream-collect` 为 107/1893。

自动测试覆盖 Learn 原子追加与重复/损坏、Init 零目标写 preview 与 missing-only/回滚、Plan 批次 receipt/父任务 marker/提升与下游真源、Review 单章/范围 ledger/选择/落库恢复，以及 Write 的精确 Agent lineage、run-bound artifact、receipt 语义重放、commit/projection/postcommit/current-truth 和幂等恢复。Plan authored-conflict、Write blocking `targeted_fix` 逐 issue resolution 与作者正文/合同冲突恢复三类生产分支均已使用可信父任务 scope-bound decision receipt，并通过 stale、跨 task/scope、篡改、裸 token/字符串等对抗用例。

Setup 已在当前工作区显式 Apply：五个 `.codex/agents/*.toml` 均已创建，再次 check 为 `current`，无冲突。默认 personal marketplace 与本机插件源已建立并通过 Plugin Creator 与 package validator；Windows Store `codex.exe plugin add` 仍因 Access denied 无法从 shell 执行，所以最终安装由 Codex App 页面完成。

仍未完成的现场门：

- 以 `0.3.0+codex.local-<cachebuster>` 在 App 点击安装，并在新 Codex 顶层任务发现全部 9 个 Skills 与 5 个项目 Agent；
- Init 真实 `Apply` 与可选 reference `Adopt`；
- Review/Plan/Write 的真实父/子 rollout 和父任务用户选择；
- 真实四 Luna Agent 完整写章链，以及一次 projection 失败后的 retry/replay；
- 非 Git 项目的 backup skip 真源回读。

因此当前代码版本为 `0.3.0` 本机 Full-write Beta，只声明“M5/M6 自动核心完成、live gate 未关闭”。单独生成正文、静态 validator、fixture 或子 Agent 自报均不能关闭上述门；`v0.3.0` tag、GitHub Release、发布 Marketplace、多平台与外部用户支持仍未创建。
