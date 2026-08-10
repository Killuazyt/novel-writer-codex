# novel-writer-codex 完整移植实施计划

## 1. 项目目标与当前基线

计划文件落盘位置：

`F:\codexnovel\novel-writer-codex\docs\IMPLEMENTATION_PLAN.md`

本文件已于 2026-08-09 以 UTF-8 无 BOM 回读校验，并继续作为唯一详细实施清单；[PORTING.md](PORTING.md) 只保留简要迁移状态。M0.1–M4 的自动 gate 已通过；当前在 M5/M6 补齐本机写作必需的用户裁决与事务恢复。自 2026-08-09 起，当前交付目标由“面向其他用户发布”收缩为“本机可持续写小说”；M7、M8 中的 CI、Marketplace、多平台、外部安装、tag 与 release 暂缓，不再阻断本机可用。

### 当前交付目标：本机可持续写小说

当前代码版本定为 `0.3.0` 本机 Full-write Beta，对应已经落地的 M6 自动核心；本机安装副本使用 `0.3.0+codex.local-<cachebuster>` 触发 Codex 刷新。该版本号不表示已创建 `v0.3.0` tag、GitHub Release 或面向其他用户发布。

当前只以本机 Windows Codex Desktop、当前用户和本地小说项目为支持范围。完成定义是：

- 本机能够发现 9 个 `$webnovel-*` Skill，并由 `$webnovel-setup` 安装、校验 5 个项目 Agent。
- 能在不手拼 Python/shell 命令的正常对话中完成：初始化/打开小说、规划、查询、学习、审查、写章和 Dashboard。
- Plan authored-conflict、Review blocking、Write blocking `targeted_fix`、作者正文/合同冲突均使用当前父任务的可信有限选择 receipt；未回答、跨任务、过期或篡改 receipt 必须 fail-closed。
- 至少完成一次真实本机 Init → Plan → Review/Write 链；当前写章的 context、writer、reviewer、data 必须实际为 `gpt-5.6-luna` / `high`，父任务不得代写或静默 fallback。
- 章节提交、五项 projection、postcommit 和非 Git backup skip 能从真源回读；现场 projection 失败只补 projection，不重跑正文或 Agent。
- Windows 中文、空格、括号、`&` 路径可用；旧小说数据合同不迁移，新流程不写 `.claude`。
- 保留隔离全量测试、UTF-8/BOM、事实数据保护和 `git diff --check` 等本机安全 gate。

当前明确不要求：Ubuntu/Python 跨平台矩阵、受限 symlink 正式能力矩阵、GitHub Actions、Repo Marketplace、外部用户安装/升级/卸载、上游同步演练、clean archive、tag、release、push 或 GitHub 上传。Git backup live smoke 也不是本机非 Git 写作的完成前提；如以后启用，仍需用户单独授权。

### 长期保留目标（本机稳定后再恢复）

把上游 `lingfengQAQ/webnovel-writer` 移植为 Codex 专用插件，在不迁移小说数据、不改变 Story System 业务契约的前提下，实现：

- 9 个 Codex Skill：保留 8 个 `$webnovel-*` 名称，新增 `$webnovel-setup`。
- 5 个项目级 Codex Agent，其中新增独立 `webnovel_writer`，把正文起草与润色从主对话中剥离。
- 原有初始化、规划、查询、学习、审查、写章和 Dashboard 功能。
- 用户可在 Codex 对话中使用自然语言或显式 `$webnovel-*` 命令驱动完整流程，不需要手工拼接 Python 或 shell 命令。
- 保留上游所有有语义价值的用户裁决点：需要作者决定时暂停并给出有限选项，收到选择后再继续。
- 规划由当前主对话模型完成；写章链和审查链固定委派给 `gpt-5.6-luna` 子 Agent，不受主对话所选模型影响。
- Windows 中文路径完整支持。
- GitHub 仓库及 Repo Marketplace 分发。
- 独立 SemVer、CI、上游漂移检查和可重复发布流程。

### 已锁定决策

- 只维护 Codex 下游，不继续维护 Claude Code 主路径。
- `.claude` 配置只允许兼容读取，任何新流程都不得写入。
- `.story-system`、`.webnovel`、`正文`、`设定集`、`大纲`保持完全兼容，不做数据迁移。
- 专用 Agent 采用项目级 `.codex/agents/*.toml`，由 `$webnovel-setup` 显式安装。
- Agent 缺失或版本不匹配时阻断相关 Skill，不静默降级为主 Agent 模拟。
- 主对话只负责理解命令、规划、编排、展示状态和向用户提问；不得自行代写正文、润色正文或伪造审查结论。
- `$webnovel-plan` 始终使用任务创建时用户选定的主对话模型，不为规划固定另一个模型。
- `$webnovel-write` 调用的 context、writer、reviewer、data Agent，以及 `$webnovel-review` 调用的 reviewer，固定 `model = "gpt-5.6-luna"`、`model_reasoning_effort = "high"`；父会话模型或 reasoning effort 不得覆盖它们。
- `webnovel_deconstruction_agent` 服务于初始化与创意分析，继承主对话模型；它不参与正文起草、润色或章节审查。
- 指定 Agent、模型或 reasoning effort 不可用时必须阻断并报告 `model_unavailable` 或 `agent_unavailable`，禁止回退到父模型或其他模型后继续产出。
- 子 Agent 隔离的优化目标是减少主对话上下文污染和昂贵主模型 token；由于每个子 Agent 都会产生独立调用，不承诺全链路 token 总量一定低于单 Agent。
- 创作/流程裁决与系统权限审批分离：前者由 Skill 给出 2–3 个有限选项并等待用户回答，后者交给 Codex 的 permission/approval 机制，二者不得互相冒充。
- `$webnovel-init` 默认不初始化 Git，必须询问用户。
- `$webnovel-review` 先交付单章，1.0 前补齐一次最多 5 章的串行范围审查。
- 文档以中文为主，命令、文件名、Schema 字段保持英文。
- 每个里程碑验收后先汇报，经用户确认再提交；不自动 push、tag 或发布。

### 2026-08-06 基线

| 项目 | 当前状态 |
|---|---|
| Git | `main` 已初始化，`origin` 已配置，尚无提交 |
| 上游 | `master@2041abad78211e29a67a2f0c64b2a97a747dce57`，版本标记 `6.2.1` |
| 文件 | 原 4 Agent 方案目标 346 个有效文件；320 个与上游同哈希，10 个已适配，62 个待迁，16 个 Codex 新增；新增 writer 后由 M3 重算 package allowlist 与目标数 |
| Skills | `0/9` |
| Agents | `0/5` |
| 测试 | 收集 746 项；当前全量套件含 Claude 契约且可能访问真实 `~/.claude`，不能直接作为 CI |
| 已通过 | Codex Adapter、Plugin manifest、Hook 单测、核心 commit/projection/write-gate 定向测试 |
| 发布能力 | 尚无 CI、Marketplace、release note、上游 lock 或真实安装 smoke |

### 2026-08-07 M3 本地状态

| 项目 | 当前状态 |
|---|---|
| 文件 | 387 个有效文件：261 个与冻结上游同哈希、69 个已适配、57 个 Codex 新增；上游 Claude Agent 树已在 M3 完成职责迁移，剩余 48 个上游 Skill 文件留待 M4–M6 |
| Skills | `1/9`：`$webnovel-setup` 已实现，其余 8 个业务 Skill 未开放 |
| Agents | `5/5` 合同与 TOML 生成完成；实际项目安装后回读 `current` |
| 测试 | 安全收集 1055 项；`full` 为 978 passed、2 skipped、75 deselected；coverage 90.50% |
| 真实模型 | Sol/Terra 两个父任务下共 8 个固定角色 rollout 均为 `gpt-5.6-luna / medium`；父任务独立规划 smoke 工具调用为 0 |
| M3 blocker | 无；`/hooks` 未信任→持久化信任两阶段现场证据尚未采集，但仅作为可选安全增强保留；CLI WindowsApps 探针因 WinError 5 blocked，未冒充 live pass |

### 2026-08-08 M4 本地状态

| 项目 | 当前状态 |
|---|---|
| 文件 | 冻结上游 Doctor/Query/Dashboard 共 6 个 Skill 文件已适配；其余 42 个上游 Skill 文件留待 M5–M6 |
| Skills | `4/9`：Setup、Doctor、Query、Dashboard 已实现；其余 5 个业务 Skill 未开放 |
| 测试 | 安全收集 1132 项；`full` 为 1042 passed、3 skipped、87 deselected；coverage 90.34% |
| Windows smoke | 中文、空格、括号、`&` 路径下 Doctor/Query 安全边界及 Dashboard 动态端口、两个 200、穿越 403、stop、事实零变化通过 |
| 发布门 | 真实安装后的新 Codex 顶层任务发现证据因本轮禁止创建顶层任务而未采集；0.1.0 未发布 |

### 2026-08-08 M5/M6 本地状态

| 项目 | 当前状态 |
|---|---|
| Skills | `9/9` 源适配已落地；Learn、Review 单章、Init 与 Plan 绿地路径的自动实现完成；Review 范围实现完成；Write 仍是部分实现 |
| M5 | Learn、Review 单章、Init、Plan 的 schema、路径、锁、原子写、回滚、receipt 与 current-truth 自动 gate 已通过；Plan authored-conflict 可信 decision receipt 已实现，真实父/子 rollout 与用户选择现场证据待采集 |
| M6 | default/fast/minimal 写章事务、四 Agent 精确 lineage、run-bound artifact、blocking `targeted_fix` 逐 issue resolution、作者正文/合同冲突恢复、commit/projection/postcommit/backup 真源回读与断点恢复均已自动实现；完整 live 链仍待完成 |
| 测试 | 该轮历史证据为安全收集 1821 项、1699 passed、15 skipped、107 deselected；2026-08-09 最新结果见下方本机可用状态 |
| 发布门 | 未创建新 Codex 顶层任务，未执行真实 Git backup/tag，未做 Ubuntu/symlink 能力矩阵；未 commit、push、tag、release 或上传 GitHub |

### 2026-08-09 本机可用自动实现状态

| 项目 | 当前状态 |
|---|---|
| M5/M6 实现 | Plan authored-conflict、Write blocking `targeted_fix` 逐 issue resolution、作者正文/合同冲突恢复三类可信父任务 receipt 已完成；保留/取消/仅状态具有可恢复 terminal overlay |
| 测试 | 2026-08-10 `full` 为 1854 passed、15 skipped、107 deselected；coverage 90.23%；9 个真实宿主保护路径零变化；adapter/package/Plugin Creator、Skill、UTF-8/BOM 与 `git diff --check` 均通过 |
| 本机 Setup | H2 合同修正后用户已授权 Apply；只更新 `webnovel_context_agent.toml`，自动备份已生成，其余 4 个 Agent unchanged，复查 5/5 `current` |
| 本机插件发现 | App 已安装 `0.3.0+codex.20260809171729`；独立新顶层任务 `019fe995-73ef-7031-a10a-2d243e2730bf` 从该缓存发现 9/9 Skill，并由缓存 runtime 复查 5/5 Agent `current`、0 conflict。Plugin Creator validator 与受检目录前后指纹均通过，零写入 |
| 本机真实验收 | `write-ch0001-737f9df2a045` 已完成第 1 章 default 全链，context/writer draft/reviewer/writer polish/data 均由真实 `gpt-5.6-luna / medium` Agent 产出；accepted commit、postcommit、non-Git backup skip 与 15 个事务阶段均由真源回读，主任务未代写 |
| 本地 RAG 与后续流程 | 默认 `Qwen/Qwen3-Embedding-0.6B` 本地后端，无 `EMBED_API_KEY`；实际离线 1024 维推理和 vector-only retry 写入 14 条向量成功。Doctor 0/0、独立 full Review 0 issue/0 blocker、Query 无 fallback、Learn 回读、Dashboard loopback 双 200/stop 均通过 |
| rollout / Desktop child 绑定修复 | 重复 `session_meta`/turn 只按既定安全规则合并；Write/Review/Init 从 canonical marker 派生完整 SHA-256 base32 task name，并强制严格整数 `depth=1` 与精确 `/root/<task_name>`。当前宿主无明文 prompt 时只收唯一 final，旧 marker 分支不再把 commentary 当结果；Write receipt 不可降级绕过，Init Apply/Adopt 只认 top-level 父任务，Review decide/persist/resume 均重验 receipt；真实 Review 另发现并修复安装形态 `review_pipeline` 导入问题 |
| 暂缓项 | CI、发布 Marketplace、Ubuntu/其他平台、外部用户安装、commit/push/tag/release 与 GitHub 上传继续不属于当前完成定义 |

官方约束以当前的 [插件打包](https://developers.openai.com/plugins/build/plugins)、[Skill](https://learn.chatgpt.com/docs/build-skills)、[项目级 Agent](https://learn.chatgpt.com/docs/agent-configuration/subagents)、[GPT-5.6 Luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna)和 [Hooks](https://learn.chatgpt.com/docs/hooks)文档为准。

## 2. 架构与公共接口

### 不可破坏的系统边界

- Python runtime、Story System、commit、projection、memory 和 review pipeline 是业务真源。
- Skill 和主 Agent 不得直接改写：

  - `.story-system/commits/**`
  - `.webnovel/state.json`
  - `index.db`
  - summary、memory、vector、projection log 等受保护投影

- 受保护内容只能经现有 runtime gate、chapter-commit 和 projection writer 更新。
- Hooks 是纵深防御；Hooks 未信任、禁用、超时或 Python 不可用时，runtime gate 仍必须阻止非法事务。
- 不自动安装依赖、打开浏览器、访问网络、初始化 Git、创建提交或修改父仓库。
- 所有中文 Markdown、JSON、YAML、TOML、TXT 使用显式 UTF-8；Skill、YAML、TOML 使用 UTF-8 无 BOM。

### 项目定位与配置优先级

`WEBNOVEL_HOME` 解析为：

1. 显式环境变量 `WEBNOVEL_HOME`
2. `<CODEX_HOME>/novel-writer-codex`
3. `~/.codex/novel-writer-codex`

项目根解析顺序固定为：

1. CLI `--project-root`
2. `WEBNOVEL_PROJECT_ROOT`
3. 从当前目录向上查找 `.webnovel/state.json`
4. 当前工作区 `.codex/.webnovel-current-project`
5. `<WEBNOVEL_HOME>/workspaces.json`
6. 旧 `.claude/.webnovel-current-project` 和旧 Claude registry，只读 fallback

新流程只写：

- `<workspace>/.codex/.webnovel-current-project`
- `<WEBNOVEL_HOME>/workspaces.json`

其中 M1 已完成 `WEBNOVEL_HOME`、native registry 与 `.env` 优先级；M2 已完成工作区 pointer 的 Codex 化、解析来源标记和旧宿主只读兼容。

`.env` 优先级固定为：进程环境变量 → 小说项目 `.env` → `<WEBNOVEL_HOME>/.env` → 旧 Claude `.env` 只读 fallback。任何诊断输出都不得打印密钥值。

`where --format json` 新增稳定字段：

```json
{
  "schema_version": 1,
  "project_root": "...",
  "resolved_from": "cli|env|cwd|codex_pointer|codex_registry|legacy_pointer|legacy_registry",
  "compatibility_mode": "native|legacy_read_only"
}
```

CLI 统一退出码：

- `0`：成功或状态已满足
- `1`：发现 blocker、配置需更新或可处理冲突
- `2`：命令本身执行失败或输入非法

### `$webnovel-setup` 与项目 Agent

新增接口：

```text
python -X utf8 scripts/webnovel.py codex-setup
  --workspace-root <PATH>
  [--check | --apply]
  [--format text|json]
```

规则：

- 默认等同 `--check`，零写入。
- `--apply` 必须由 Skill 在用户确认后调用。
- 创建以下项目 Agent：

  - `webnovel_context_agent`
  - `webnovel_writer`
  - `webnovel_reviewer`
  - `webnovel_data_agent`
  - `webnovel_deconstruction_agent`

- Agent 角色合同以 `references/agents/*.md` 为唯一语义真源，由生成器产生 `.codex/agents/*.toml`。
- context、writer、reviewer、data 的 TOML 固定 `model = "gpt-5.6-luna"`、`model_reasoning_effort = "high"`；deconstruction 不写这两个字段并继承父会话。
- context、reviewer、deconstruction 使用 `read-only`；writer、data 使用 `workspace-write`。
- writer 只能在 `.webnovel/tmp/write-runs/<run_id>/` 写入本轮 `draft.md`、`polished.md` 和最小 manifest；不得直接写最终正文、Story System 或其他 canon。data 只允许生成三份既定 `.webnovel/tmp` artifact。
- 管理记录保存到 `.codex/novel-writer-codex/managed-agents.json`。
- 已管理但过期的 Agent，先备份到 `.codex/novel-writer-codex/backups/<timestamp>/` 再更新。
- 同名但不受本插件管理的 Agent 一律拒绝覆盖；不提供宽泛 `--force`。
- Apply 完成后输出 `restart_required=true`，要求用户打开新的 Codex 任务。
- Setup 不创建小说项目、不写 `.claude`、不修改任何小说事实数据。

JSON 结果包含：

```json
{
  "schema_version": 1,
  "status": "current|changes_required|applied|conflict|failed",
  "workspace_root": "...",
  "created": [],
  "updated": [],
  "unchanged": [],
  "conflicts": [],
  "backup_dir": null,
  "restart_required": false
}
```

Agent 使用规则：

- `review` 必须发现 reviewer。
- `write default/fast` 必须发现 context、writer、reviewer、data。
- `write minimal` 仍必须发现 context、writer、data，只跳过 reviewer。
- `init` 无参考作品时可独立运行；提供参考文本时必须发现 deconstruction。
- 缺失或哈希过期时统一阻断，并引导 `$webnovel-setup` → 新任务；禁止兼容模式冒充专用 Agent。

### 对话交互与模型路由合同

用户只需要在 Codex 主对话中提出“初始化小说”“规划第 N 卷”“写第 N 章”“审查第 N 章”等请求；主 Agent 负责把自然语言或显式 `$webnovel-*` 调用路由到对应 Skill。Skill 内部命令是实现细节，不要求用户手工执行。

需要作者裁决时，由主 Agent 统一提问，子 Agent 不直接与用户争夺对话控制：

- 每次只问 1–3 个短问题；每题提供 2–3 个互斥选项，把推荐项放在第一位并说明影响，同时允许用户自由输入。
- 当前客户端提供结构化选择控件时优先使用；未提供时退化为语义等价的编号选项并等待回答，而不是擅自采用默认值。
- Setup apply、初始化写入/Git、规划冲突或覆盖、创作方向分歧、不可自动修复的 blocking issue、作者手改正文恢复和范围审查是否继续，均属于必须等待用户的裁决点。
- 无语义分歧、可安全重试或纯状态提示不重复询问，避免确认疲劳。

模型路由固定为：

| 工作 | 执行者 | 模型来源 | 主对话可见内容 |
|---|---|---|---|
| 理解命令、规划、用户裁决、流程编排 | 主 Agent | 当前对话模型 | 需求、计划、状态和精简汇总 |
| 写前上下文压缩 | `webnovel_context_agent` | 固定 `gpt-5.6-luna` / `high` | 仅任务书摘要与 artifact 引用 |
| 正文起草、定点修复、润色 | `webnovel_writer` | 固定 `gpt-5.6-luna` / `high` | 路径、hash、字数和状态，不回传整章正文 |
| 单章/范围章节审查 | `webnovel_reviewer` | 固定 `gpt-5.6-luna` / `high` | 结构化问题摘要和 artifact 引用 |
| 写后事实提取 | `webnovel_data_agent` | 固定 `gpt-5.6-luna` / `high` | 三份 artifact 的路径、hash 和校验状态 |
| 初始化参考作品拆解 | `webnovel_deconstruction_agent` | 当前对话模型 | 精简候选与风险摘要 |

每次 Agent 调用必须在 run ledger 中记录 `agent_name`、请求的 `model`、实际报告的 `model`、reasoning effort、输入 artifact/hash、输出 artifact/hash 和状态。实际模型与合同不一致时，本次结果作废并阻断；不得把非 Luna 产物写入正文、审查报告或事实提交链。

### 其他新增或调整接口

- Init：

  ```text
  webnovel.py init --config-json <PATH> [--dry-run|--apply]
    --git-mode off|init|initial-commit
  ```

  `--git-mode` 默认 `off`。Skill 必须展示目标路径和写入清单，再询问 Git 选项和最终确认。预览阶段不得修改目标小说目录；临时请求文件只能落在 `<WEBNOVEL_HOME>/tmp/init/` 并明确报告。

- Learn：

  `project-memory add-pattern` 增加 `--input-json <PATH>`，避免把用户文本拼入 shell。输入 JSON 包含 `pattern_type`、`description`、`category`、`importance` 和可选 `source_chapter`。

- Dashboard：

  ```text
  webnovel.py dashboard start --project-root <PATH> --host 127.0.0.1 --port 0 --no-browser
  webnovel.py dashboard status --project-root <PATH>
  webnovel.py dashboard stop --project-root <PATH>
  ```

  PID、日志和进程状态存放在 `<WEBNOVEL_HOME>/runtime/dashboard/<project_hash>/`，不得写小说数据。默认动态端口、localhost、no-browser。

- Plan：

  新增确定性的 `plan-validate --volume <N> --format json`。只有所有校验通过后，才能调用现有 master-outline、state 和 Story System 更新接口。

- Review：

  - 第一阶段只接受单个正整数章节号。
  - 完整版接受 `<start>-<end>`，包含首尾且最多 5 章。
  - 范围审查逐章串行，每章拥有独立 run ID、临时 artifact、报告及恢复状态；默认在第一个 blocker 或错误处停止。

- Upstream：

  新增 `scripts/upstream_sync.py check|prepare`。`check` 只生成漂移报告；`prepare` 只写 `.tmp/upstream-sync/<sha>/` 暂存区，绝不直接覆盖工作树。

## 3. 分阶段实施

### M0.1：计划落盘与安全入口

任务：

- [x] 以 UTF-8 无 BOM 回读本计划，并保持它为唯一详细实施清单。
- [x] 从 README、PORTING 和 Codex 支持说明链接本计划。
- [x] 在 M1 完成前只列安全定向测试；M1 完成后统一要求隔离 runner 或显式 bootstrap。
- [x] 删除“缺少专用 Agent 时由主 Agent 模拟”的兼容说法，统一改为阻断并引导 `$webnovel-setup`。

验收：

- 三处入口均能定位本计划。
- 文档不建议直接执行未隔离的原始全量 pytest。
- 中文回读正常，无 BOM 或乱码。

### M0：冻结派生基线与保护边界

任务：

- [x] 创建 `upstream-lock.json`，锁定 `lingfengQAQ/webnovel-writer`、`master`、完整 SHA、版本 `6.2.1`、330 个文件及逐文件 SHA-256。
- [x] 按 UTF-8 路径排序，以 `path + NUL + lowercase_hash + LF` 验证总哈希 `3760c0c6cbc1f1b90116785e3885b5e1480f468f2f8306b7a8a62066e9336e60`。
- [x] 添加只读 `upstream` remote；fetch 指向上游，push URL 为 `DISABLED`，并复核远端 `master` 仍为冻结 SHA。
- [x] 明确 exclude 与外层测试脚手架归属；Agents/Skills 标为 M3 待迁，不作为永久删除项。
- [x] 保持最小 `.codex-plugin/plugin.json`，继续使用默认 `hooks/hooks.json` 自动发现。
- [x] 增加显著的派生修改、GPL-3.0、上游作者与冻结提交归属说明。
- [x] 将 `.webnovel/state.json`、`.webnovel/summaries/**` 纳入 hook 保护范围，并覆盖拒绝与合法 runtime 写入测试。
- [x] 修复混合换行、Markdown 尾空格、EOF 多余空行及 `scripts/sync_plugin_version.py` BOM，补充文本 LF 规则。
- [x] 使用临时 Git index 做 whitespace 检查，不污染真实 index。
- [x] 清理 `.tmp`、`.pytest_cache`、`__pycache__`、`.coverage`，不删除其他未跟踪基线文件。

验收：

- manifest、hooks、lock JSON 可解析。
- Adapter、Plugin Creator validator、Hook 定向测试通过。
- `git diff --check` 通过。
- secret scan 和 archive allowlist 无异常。
- README 不宣称尚未实现的 Skill 已可用。

建议提交，经用户确认后执行：

`chore: establish Codex port scaffold from upstream 2041abad`

### M1：P0 测试隔离与宿主契约拆分

任务：

- [x] 统一解析 `WEBNOVEL_HOME`：显式变量 → `<CODEX_HOME>/novel-writer-codex` → `~/.codex/novel-writer-codex`。
- [x] native registry 固定为 `<WEBNOVEL_HOME>/workspaces.json`；新代码只写 native，读取时 native 优先、旧 Claude registry 只读 fallback。
- [x] `.env` 按进程环境、项目、`<WEBNOVEL_HOME>`、旧 Claude 只读 fallback 的顺序加载。
- [x] 在 pytest 导入测试模块前建立唯一隔离树，重定向 home、临时目录、AppData/XDG、Git 全局配置和 coverage，并清除真实模型密钥与项目定位变量。
- [x] 使用 pytest teardown 与外层 PowerShell `finally` 双层保护 9 个真实用户关键路径；不读取 auth、session 或 cache。
- [x] 添加每项 30 秒超时和 IPv4/IPv6/DNS 默认禁网；保留 `AF_UNIX` 与 `socketpair`，Python 子进程继承策略。
- [x] 注册八类 markers，并保证每个测试恰有一个 `runtime`、`codex_contract` 或 `upstream_contract` 主 marker。
- [x] 默认表达式固定为 `(runtime or codex_contract) and not model_eval`，不默认排除 integration、failure、windows 或 slow。
- [x] 把四组 Claude 布局断言移入 `upstream_contract`，M1 只安全收集，不使用 `-k not ...` 制造假绿。
- [x] preflight 改为 runtime/CLI/context/项目定位/Story Runtime 能力检查，并输出 `webnovel-preflight/v1` 结构化结果。
- [x] `PrewriteValidator` 将 state 与 placeholder 扫描异常转为 blocker；Codex adapter validator 固定单个 JSON 对象与 `0/1/2` 退出码。
- [x] 扩展测试 runner 为 `smoke`、`collect`、`full`、`upstream-collect`；`full` 只执行当前 Codex 有效全集。
- [x] 所有 skip/xfail 写明原因、阶段和删除条件。

验收：

- 全量收集不会挂住或访问真实用户 registry。
- 用户目录及仓库外目录前后 hash/mtime 不变。
- validator 失败返回结构化错误，不 traceback。
- Codex 当前有效测试集全绿。
- 在 M1 完成前，不再直接运行未隔离的全量 pytest。

建议提交：

`test: isolate user state and split host contracts`

### M2：宿主中立 runtime 与上游同步工具

任务：

- [x] 完成工作区 pointer 的 Codex 化和其余 runtime 宿主中立化。
- [x] 所有剩余 pointer 与 runtime 状态改写 `.codex`/`WEBNOVEL_HOME`；native registry 已在 M1 完成。
- [x] `.claude`、`CLAUDE_*` 仅保留只读 fallback，并在结果中标记 `legacy_read_only`。
- [x] 插件根与参考资料通过调用文件位置自定位，不依赖 `PLUGIN_ROOT`；未来 Skills 的同一约束已纳入校验，生产 Skills 仍留在 M3。
- [x] 修复版本、package、release validator 中的 `.claude-plugin`、嵌套仓库路径和旧插件名硬编码。
- [x] 建立 `upstream_sync.py` 及 lock 漂移测试。
- [x] 把 Bash-only 语法、Claude slash command、Claude 工具名纳入静态扫描。

验收：

- Windows 中文、空格、括号、`&` 和跨盘路径通过。
- 显式 `--project-root` 不会被旧 registry 覆盖。
- 不会误命中上一本书、插件缓存目录或父仓库。
- 旧项目无需迁移即可读取，新运行只写 Codex 路径。
- runtime `scripts/data_modules` 覆盖率保持不低于 90%。

建议提交：

- `refactor(runtime): add host-neutral path and config resolution`
- `test(packaging): add Codex package and upstream drift validators`

### M3：Skill 公共框架、Setup、交互合同与五个 Agent

任务：

- [x] 新增 `$webnovel-setup`。
- [x] 为每个 Skill 保留仅含 `name`、`description` 的 frontmatter。
- [x] 为每个 Skill 增加 `agents/openai.yaml`，default prompt 显式使用 `$skill-name`。
- [x] 建立共享 Codex runtime 调用说明，去除 `export`、`$PWD`、`$()`、`cat/test/find/seq/printf`、`/dev/null` 及 shell 循环。
- [x] 移植上游四个 Agent 合同，新增独立 writer 合同，并生成五个项目 TOML。
- [x] 在 context、writer、reviewer、data TOML 中固定 `gpt-5.6-luna` / `high`，保留 deconstruction 继承当前对话模型；生成器和 managed hash 必须覆盖模型字段。
- [x] 建立统一有限选项交互协议，并把 Claude `AskUserQuestion` 语义映射到 Codex 当前客户端可用的结构化选择或编号对话 fallback。
- [x] 加入 TOML/合同哈希漂移校验和 Setup 幂等测试。
- [x] 加入模型可用性、实际模型回读和禁止父模型 fallback 的真实新任务 smoke；只解析 TOML 不算通过。
- [x] 将 Hooks 未信任→持久化信任现场 smoke 保留为可选安全增强且不再阻断里程碑；runtime、受保护路径、合同 hash 与 schema 安全边界继续作为强制自动 gate。

当前状态：M3 代码、合同、fixture、自动 gate、真实双父模型路由与父任务独立规划 smoke 均已通过，M3 标记为 complete。`/hooks` 持久化信任现场 smoke 尚未采集，作为可选安全增强保留；若补做，仍不得用 `--dangerously-bypass-hook-trust`、静态 TOML、canned fixture 或子 Agent 自报替代真实证据。

Agent 验收：

- context：只返回完整任务书，零写入，缺少事实时返回 blocker。
- writer：只根据最小任务包在本轮 staging 目录生成起草/润色 artifact，返回路径、hash、字数和状态，不向主对话回传整章正文，不直接改 canon。
- reviewer：严格 JSON、五维字段齐全、无评分；非法 JSON 最多重试一次，之后阻断。
- data：只生成 `fulfillment_result.json`、`disambiguation_result.json`、`extraction_result.json`。
- deconstruction：书名但无可靠正文时必须 `quality.passed=false`，不得编造或创建 canon。
- 五个 Agent 都通过伪系统提示、忽略指令等 prompt-injection fixture。
- 用至少两个不同的父会话模型分别触发 write/review smoke，实际 writer/reviewer/context/data 始终报告 `gpt-5.6-luna`；再用错误/不可用模型 fixture 验证阻断且不产生可提交 artifact。
- 规划 smoke 不启动 writer/reviewer，规划内容由父会话模型生成；切换父会话模型不会改变写章/审查模型路由。
- 用户裁决 fixture 覆盖结构化选择与编号 fallback；未收到回答前不得继续写入，收到回答后只能执行所选分支。
- TOML sandbox 不能作为唯一保护；runtime、路径校验和前后哈希共同构成边界。

建议提交：

- `feat(setup): add explicit Codex project agent provisioning`
- `feat(agents): add canonical Codex agent contracts`

### M4：Read-only Alpha，目标版本 0.1.0

迁移：

- [x] `$webnovel-doctor`
- [x] `$webnovel-query`
- [x] `$webnovel-dashboard`

M4 验收时状态：源码实现、Skill/runtime 合同、隔离 full gate 和 Windows 中文路径真实 loopback smoke 均已通过。由于禁止创建 Codex App 顶层任务，“真实安装后的新任务发现”证据没有采集且不以静态 validator 或子 Agent 自报替代；所以 M4 实现完成，但发布门尚未全部关闭，不创建 `v0.1.0`。后续 M5/M6 的本地实现不倒填或伪造这条 live 证据。

Doctor 验收：

- 无项目、初始化完成、规划阶段、写作阶段均能正确识别。
- `--chapter`、`--deep`、JSON/Text 输出有效。
- 返回码 1 表示发现 blocker，不视为 Skill 崩溃。
- 不自动修复、安装依赖、启动 Dashboard。
- 执行前后项目事实文件 hash 不变。

Query 验收：

- 覆盖实体状态、关系、世界规则、open loops、综合上下文五类 fixture。
- 修正上游错误调用：世界规则使用 `query-rules [--domain]`，章节上下文使用 `load-context/read-summary`。
- 输出来源、路径和必要行号。
- 使用旧投影 fallback 时明确标注，不伪装成完整 Story System 数据。
- 中文实体名、引号、换行和 PowerShell 元字符安全。

Dashboard 验收：

- start 快速返回 URL、PID、日志路径；status/stop 正常。
- `/api/project/info` 和 `/api/story-runtime/health` 返回 200，不再检查不存在的 `/api/preflight`。
- 路径穿越返回 403。
- 默认 localhost、no-browser；Windows 后台进程不弹额外窗口。
- 端口占用、缺前端、缺可选依赖时给出明确错误且不联网安装。
- 运行前后小说事实数据不变。

发布门：

- [x] 三个只读 Skill 已在真实安装后的独立新 Codex 顶层任务中发现；同一任务实际加载 `0.3.0+codex.20260809171729` 缓存并完成只读 Setup 回读。
- [x] Windows 中文路径 Doctor/Query/Dashboard smoke 通过。
- [x] runtime、路径、合同 hash、schema 与事实数据前后哈希强制门通过；Hooks 未信任/已信任两阶段现场 smoke 继续作为可选增强。
- [ ] 达标并另获用户授权后才允许创建 `v0.1.0`；本轮不创建。

建议提交：

- `feat(skills): port doctor and query`
- `feat(dashboard): add safe lifecycle workflow`

### M5：Controlled-write Beta，目标版本 0.2.0

迁移：

- [x] `$webnovel-learn` 自动实现与合同测试。
- [x] `$webnovel-review` 单章版自动实现、严格 ledger、有限选择和落库恢复。
- [x] `$webnovel-init` missing-only preview/apply、回滚与 Git 边界自动实现。
- [x] `$webnovel-plan` 绿地规划、批次 receipt、parent-only validate、提升、回滚与真源 status。
- [x] `$webnovel-plan` authored-conflict 的可信父任务有限选择 receipt 自动实现与对抗测试；现场验证仍列在下方 live gate。

Learn 验收：

- 用户文本通过 JSON 输入，不进入 shell 拼接。
- 首次写入、追加、完全重复跳过、损坏 JSON 拒绝均正确。
- 保留旧记录、锁、原子替换和 UTF-8。
- 新经验能被后续 `memory-contract load-context` 实际读取。

Review 验收：

- reviewer 严格输出 setting、timeline、continuity、character、logic 五维结果。
- reviewer 的 run ledger 实际模型必须为 `gpt-5.6-luna` / `high`；不匹配时不生成报告或落库。
- 每个 issue 有 evidence、fix hint 和 blocking 标记。
- metrics JSON、报告和数据库一致。
- 不复用上一章 tmp artifact。
- blocking 后只允许“定点修复”“仅保存报告”“放弃”三类用户裁决；未授权不得改正文。
- 落库失败从落库步骤恢复，不重跑 reviewer。

Init 验收：

- 最终确认前不修改目标小说目录。
- 拒绝空 slug、点目录、插件目录、路径逃逸和错误父仓库。
- `--git-mode` 默认 off；init/initial-commit 都必须显式选择。
- Git 只作用于解析后的小说根，绝不沿用父仓库。
- state、idea bank、总纲、设定集和 MASTER_SETTING 一致。
- 可选参考文本视为不可信数据；只有用户确认后的高置信拆解才能进入 canon。
- 重跑只补缺失项，不覆盖用户修改，具有幂等性。
- 完成后能通过 plan 前置检查。

Plan 验收：

- 规划只在当前主对话执行，继承用户创建任务时选定的模型；不得调用 writer/reviewer 代替规划，也不得把规划模型固定为 Luna。
- 输出卷节拍表、卷时间线、详细大纲和总纲写回。
- 每章具有时间字段、1 个 CBN、2–4 个 CPN、1 个 CEN 及最多 4 个必须覆盖节点。
- 时间单调、倒计时正确、相邻 `CEN → CBN` 承接。
- 规划范围内每章均生成 volume/chapter/review runtime contract。
- blocker 未裁决或 `plan-validate` 失败时，不更新 state 和 Story System。
- 批次失败只重做失败批次，不覆盖已通过结果。
- 目标首章最终通过 `write-gate --stage prewrite`。

M5 尚未关闭的现场/发布门：

- [x] 在真实安装后的独立新 Codex 顶层任务中发现全部 M5 Skills；证据来自该任务实际加载的安装缓存，不以当前任务文件、validator 或子 Agent 自报替代。
- [x] Init 在真实父 rollout 中完成一次 `Apply`；`git-mode off`、目标零写入预览、父任务绑定授权和临时文件删除均由真源回读。
- [ ] 可选参考路径另需真实 deconstruction 子 Agent 与 `Adopt`/`Discard`/`Cancel` 用户回答证据。
- [ ] Review 以真实 `gpt-5.6-luna / high` reviewer 完成单章，并验证 blocking 三选一的父任务用户回答 receipt。
- [x] Plan 由当前真实父模型完成 marker/validate/greenfield apply、10 章合同提升与首章 prewrite。
- [ ] 对已实现的 Plan authored-conflict receipt 做覆盖现场 smoke。

建议提交：

- `feat(learn): port safe project learning`
- `feat(review): port structured single-chapter review`
- `feat(init): port confirmed project initialization`
- `feat(plan): port validated volume planning`

### M6：Full-write Beta，目标版本 0.3.0

迁移：

- [x] `$webnovel-write`：default/fast/minimal、blocking 定点修复逐 issue receipt、作者正文/合同冲突恢复与真源恢复自动实现；完整 live 链未验收。
- [x] `$webnovel-review` 一次最多 5 章、逐章串行且可恢复的范围审查自动实现。
- [ ] 全事务故障恢复：自动 fault injection 与 commit 后幂等恢复已覆盖，仍缺真实 projection 失败/retry、作者裁决和 Git backup 现场链。

写章顺序固定，不允许并步：

```text
preflight
→ contract refresh / prewrite
→ context agent
→ writer agent draft staging
→ reviewer
→ review-pipeline
→ writer agent targeted fix / polish staging
→ data agent artifacts
→ precommit
→ chapter-commit
→ five projections
→ postcommit
→ backup
```

模式：

- default：完整上下文、五维 reviewer、完整润色和全部 gate。
- fast：reviewer 只执行 setting/timeline/continuity；其他维度显式标记 skipped。
- minimal：跳过 reviewer 和 anti-AI 深检，但必须生成本章新的 no-review artifact；context、data、事务 gate 仍不可跳过。

验收：

- reviewer 每章最多一轮；blocking 未处理不得提交。
- context、writer、reviewer、data 的实际模型都必须为 `gpt-5.6-luna` / `high`；父会话使用任何可用模型时都不得改变该路由。
- draft、定点修复和 polish 全部由 writer Agent 完成；主 Agent 只编排和提升已验证 staging artifact，禁止自行补写或改写正文。
- 主对话只接收任务摘要、artifact 路径/hash、字数、问题摘要和状态；完整任务书、正文与长审查明细留在子 Agent/artifact，避免污染规划上下文。
- 任一 Luna Agent 不可用、超时、实际模型不匹配或输出越界时立即阻断，不回退父模型；失败 staging artifact 不得提升为最终正文。
- 三份 data artifact 必须通过现有 Schema validator。
- `pending` 消歧、遗漏必写节点和 anti-AI blocker 必须阻断。
- precommit 早于 chapter-commit，postcommit 晚于 commit。
- commit 后五项 projection 全部为 done/skipped。
- projection 失败只 retry projection，不重跑正文、reviewer 或 data Agent。
- 正文被作者手改、章纲晚于正文或章节已 accepted 时必须询问恢复选择。
- 非 Git 项目允许跳过 backup，但不允许跳过事务 gate。
- Git backup 只能 add 小说项目内的明确 allowlist。
- 最终报告在 artifact、commit、projection 或 backup 缺失时不得写“已完成”。

范围审查验收：

- 接受单章或最多 5 章范围。
- 每章独立 reviewer、artifact、report、run ledger。
- 默认遇到 blocker/失败即停止；用户明确选择后才能继续其余章节。
- 重新运行从未完成章节继续，不重复落库已成功章节。

M6 尚未关闭的实现与现场门：

- [x] 为 blocking review 实现可信父任务 `targeted_fix` 选择、逐 issue resolution receipt，并只提升经过验证的 writer staging artifact。
- [x] 为作者已修改正文/合同较新等冲突实现可信 `replace_with_verified`/保留/取消 receipt；裸 CLI 字符串继续不构成授权。
- [ ] 在真实任务中完成 context、writer draft、reviewer、writer polish、data 四角色 `gpt-5.6-luna / high` 的 default/fast/minimal 链，且父任务不代写。
- [x] 现场注入一次 projection 失败，证明只 retry/replay projection，不重跑正文、reviewer 或 data Agent。
- [ ] 在用户单独授权后，以临时普通 Git 小说项目完成 exact allowlist backup/tag live smoke；本轮不对当前仓库做任何 Git 写。
- [ ] 真实范围审查逐章运行，并验证 blocker 后 `stop`/`continue` 父任务用户选择 receipt。
- [ ] 补 Windows 受限 symlink 能力用例与 Ubuntu 正式矩阵；当前 15 个 skip 不冒充通过。
- [x] 在真实安装后的独立新 Codex 顶层任务中发现全部 9 个 Skills；Hook 未信任→持久化信任 smoke 继续只作为可选安全增强。

建议提交：

- `feat(write): port transactional chapter workflow`
- `feat(review): add resumable serial range review`
- `test(write): add failure injection and resume coverage`

### 本机可用 Gate（当前交付目标）

- [x] 补齐 Plan authored-conflict、Write blocking `targeted_fix` 逐 issue resolution、作者正文/合同冲突恢复三类可信父任务 receipt。
- [x] 三类新增生产分支的定向、对抗与恢复测试通过；M5/M6 隔离 full gate、UTF-8/BOM、事实保护和 `git diff --check` 通过。
- [x] 本机显式执行 Setup Apply；5 个项目 Agent 已创建，复查为 current，且无冲突。
- [x] 以 `0.3.0+codex.20260809171729` 刷新并在 App 点击安装；独立新顶层任务已确认 9 个 Skill 可发现、5 个 Agent 均为 `current`、0 conflict，且检查零写入。
- [x] 累计完成真实本机链：`test.v.0.3` 的 Init（`git-mode off`）→ 用户 `Apply` → Plan → prewrite，以及既有验收项目的 default Review/Write；四个写章 Agent 的实际模型均为 `gpt-5.6-luna` / `medium`，父任务未代写。
- [x] 现场注入一次 projection 失败并只恢复 projection；非 Git 项目安全记录 backup skip。
- [x] 更新本机使用入口与恢复说明；所有因宿主能力或授权无法执行的 live 项逐项记录为 skipped/blocked，不用 fixture 冒充。

只有以上 gate 关闭，才可称“本机可持续写小说”。这不等于已发布版本，也不表示其他用户或平台受支持。

### M7：Release Candidate，目标版本 0.9.0（暂缓）

任务：

- [ ] 建立 GitHub Actions。
- [ ] 将 M2 已 Codex 化的 version、package、release-note validator 接入 RC/CI/Marketplace/release 发布链。
- [ ] 加入 clean archive allowlist、secret scan、依赖检查和 Dashboard dist 漂移检查。
- [ ] 创建 `.agents/plugins/marketplace.json`。
- [ ] 完成安装、升级、禁用、卸载和缓存更新测试。
- [ ] 完成上游同步演练。
- [ ] 补齐中文安装、依赖、Hook 信任、Setup、新任务及故障恢复文档。

Marketplace 条目固定为：

- `name`: `novel-writer-codex`
- `source.source`: `url`
- `source.url`: `https://github.com/Killuazyt/novel-writer-codex.git`
- `source.ref`: 当前发布 tag
- `policy.installation`: `AVAILABLE`
- `policy.authentication`: `ON_INSTALL`
- `category`: `Productivity`

开发测试允许 `--ref main`，正式版本必须 pin tag。

发布流程只允许显式 `workflow_dispatch` 或 `v*` tag 触发；普通 main push 不自动创建 tag。CI 默认 `contents: read`，仅授权发布 job 使用 `contents: write`。

建议提交：

- `ci: add isolated cross-platform regression`
- `release: add repo marketplace and release validation`

### M8：Stable 1.0（暂缓）

完成条件：

- 9 个 Skill 全部可发现，显式 `$skill` 和自然语言触发均通过。
- 5 个项目 Agent 均通过 Setup、更新、冲突和新任务发现测试。
- 对话式有限选项在 Setup、Init、Plan、Review、Write 和恢复路径均通过；系统权限审批仍由 Codex 原生 permission/approval 处理。
- 在不同父会话模型下，规划使用父会话模型，写章与审查的实际 Agent 模型始终为 `gpt-5.6-luna` / `high`，且无静默 fallback。
- 旧小说项目无需数据迁移即可打开和继续写作。
- 主路径不再依赖 Claude 配置；兼容层从不写 `.claude`。
- default/fast/minimal、单章/范围审查及失败恢复全部通过。
- Windows 中文路径和 Ubuntu 回归通过。
- 若选择执行 Hook 现场 smoke，未信任时不得误报保护已启用，信任后真实 deny 测试必须通过；该现场 smoke 本身不阻断 Stable，runtime 与数据安全 gate 仍为强制条件。
- clean tag 构建的 archive 不含密钥、用户状态、小说数据、缓存或 coverage。
- manifest、CHANGELOG、release note、Marketplace tag、UPSTREAM lock 完全一致。
- 从 Git Repo Marketplace 安装到全新缓存后，在新 Codex 任务完成 Setup → Init/打开旧书 → Plan → Review → Write smoke。
- 用户显式批准后才创建 `v1.0.0`、推送和发布。

## 4. 测试、CI 与故障矩阵

### 自动测试层级

1. 静态校验：JSON/TOML/YAML、manifest、Skill frontmatter、Agent hash、UTF-8 无 BOM、Claude-only/Bash-only 扫描。
2. 单元测试：路径解析、Setup、Hooks、Schema、锁、原子写、投影、validator。
3. 行为测试：使用 canned Agent 输出，不在 CI 调用真实模型；覆盖有限选项等待/分支、父会话模型变化、模型不匹配与禁止 fallback。
4. 集成测试：新项目、旧 Claude 配置项目、非 Git 项目、嵌套父仓库。
5. 故障注入：reviewer 非 JSON、artifact 缺失、SQLite busy、projection 中断、backup 中断、并发重复提交。
6. 人工发布 smoke：真实 Codex 安装、Hook 信任、Setup、新任务及项目 Agent；回读实际 agent/model/effort，验证规划继承父会话而 write/review 固定 Luna。

### CI 分层

| 触发 | 内容 |
|---|---|
| PR-fast | 静态校验、manifest、Hook、安全负例、Codex contracts；目标不超过 60 秒 |
| PR-cross-platform | Ubuntu Python 3.10/3.14，Windows Python 3.11/3.13，强制 UTF-8 和隔离 home |
| Main | 完整 runtime、90% 覆盖率、Windows 中文路径、Dashboard Node 20 build/dist 检查 |
| Nightly | Python 3.10–3.14 完整矩阵、失败注入、secret scan、上游 SHA drift |
| Release | clean tag archive、版本一致性、Marketplace 安装及人工 Codex smoke |

### 必须覆盖的失败场景

- 缺 contracts/章纲：prewrite 阻断且不覆盖正文。
- reviewer timeout、非法 JSON、缺维度：不进入 commit。
- writer/reviewer/context/data 的 Luna 模型不可用、名称错误、实际模型不匹配或父模型回退：不生成可提交正文/报告/artifact。
- 用户裁决未回答、回答无效或选择“放弃”：不越过对应 gate，不把推荐项当成默认授权。
- data artifact 缺失、外层 wrapper、pending 非空：precommit reject。
- commit 成功但 projection 中断：commit 不重复，只补失败 projection。
- SQLite 锁定或模拟磁盘满：旧文件仍可解析，无半写入。
- 同章并发或重复执行：只有一个有效 commit，event ID 不重复。
- Hook 禁用、未信任或超时：runtime gate 仍有效。
- Dashboard 端口占用、路径穿越和恶意 Origin：项目数据不变。
- Windows 路径示例固定包含中文、空格、括号、`&` 和 Unicode 字符。
- 测试前后真实 `~/.codex`、`~/.claude` 及仓库外目录零变化。

## 5. 执行规则、提交与完成定义

### 每一步执行协议

1. 打开本计划，确认当前唯一待执行项及前置 gate。
2. 检查本地权威源码和当前未提交改动。
3. 只实现一个可独立验收的计划项。
4. 运行该项定向测试，再运行所属里程碑 gate。
5. 回读中文文件，校验 UTF-8、无 BOM 和无乱码。
6. 更新复选框、状态和执行日志。
7. 汇报修改文件、测试结果、已知限制及下一项。
8. 里程碑完成后等待用户确认，再创建原子提交。
9. push、tag、release 和任何外部发布均另行请求授权。

禁止跳过尚未通过的 gate；若官方 Codex 协议或上游 SHA 变化，先更新 decision log、upstream lock 和受影响测试，再继续实施。

### 上游同步协议

每次同步固定执行：

1. fetch `upstream` tags。
2. 用 lock 对比目标 SHA，生成只读漂移报告。
3. `prepare` 到 `.tmp/upstream-sync/<sha>/`。
4. 只导入 runtime/resources/templates/dashboard 等 allowlist。
5. 单独提交上游内容：`chore(upstream): sync runtime to <sha>`。
6. 单独完成 Codex 调和：`fix(codex): reconcile adapters with upstream <sha>`。
7. 全量 gate 通过后才更新 accepted SHA。
8. 两个提交都需用户确认，不直接 merge 上游仓库。

### 计划执行日志模板

| 日期 | 计划项 | 状态 | Commit | 验证命令 | 结果 | 备注 |
|---|---|---|---|---|---|---|
| 2026-08-06 | M0.1 | complete | —（未提交） | UTF-8/BOM 回读；README/PORTING/support 链接检查 | pass | 本计划继续作为唯一详细清单 |
| 2026-08-06 | M0 | complete | —（未提交） | hygiene/adapter/Plugin Creator；17 hooks；临时 index `diff --check` | pass | 上游 330 文件与总哈希一致；远端 SHA 未漂移 |
| 2026-08-06 | M1 | complete | —（未提交） | collect/upstream-collect；full；failure/windows/integration | pass | 798 = 基线 746 + 本轮 52；732 passed、66 deselected；coverage 90.16%；9 个宿主路径未变化 |
| 2026-08-07 | M2 | complete | 本轮两项原子提交（见 Git） | runtime/package/upstream 定向测试；validators；collect/upstream-collect/full；UTF-8/hygiene/diff | pass | 862 collected；789 passed、73 deselected；coverage 90.19%；冻结源 330/330 且 prepare 幂等；9 个宿主路径未变化；下一项 M3 |
| 2026-08-07 | 目标补充 | complete | —（未提交） | 官方 Agent/Luna/permission 文档核对；UTF-8/BOM 回读；`git diff --check` | pass | 对话有限选项、主对话规划、Luna 写作/审查和子 Agent 上下文隔离已并入 M3–M8；未开始 M3 实现 |
| 2026-08-07 | M3 | complete | —（按用户要求未提交） | M3 定向 204 passed、2 skipped；Skill/adapter/package/hygiene；Setup 前向与实际项目回读；显式 rollout parser；collect/upstream-collect/full；UTF-8/BOM/diff | pass | 1055 collected；978 passed、2 skipped、75 deselected；coverage 90.50%；Sol/Terra 父任务下 8/8 固定角色均为 Luna/medium，父任务规划工具调用 0；用户将 Hook 现场 smoke 降为可选增强，runtime、路径、hash 与 schema 强制边界不变；未上传 GitHub |
| 2026-08-08 | M4 | implementation complete / release gate pending | —（按用户要求未提交） | Doctor/Query/Dashboard 定向；Skill Creator；adapter/package/hygiene；五 Agent 只读 check；collect/upstream-collect/full；`codex_m4_smoke.py`；PowerShell request-file injection smoke；UTF-8/BOM/diff | pass | 1132 collected；1042 passed、3 skipped、87 deselected；coverage 90.34%；Windows 中文路径动态端口、两个 200、穿越 403、stop、事实零变化通过；真实安装后的新顶层任务发现受授权限制未采集；未进入 M5、未发布、未上传 GitHub |
| 2026-08-08 | M5/M6 | M5 automated core complete / M6 partial / live gates pending | —（按用户要求未提交） | Learn/Init/Plan/Review/Write/Backup 定向与对抗；Skill Creator；adapter/package/hygiene；五 Agent 只读 check；collect/upstream-collect/full；UTF-8/BOM/diff | pass | 1821 collected；1699 passed、15 skipped、107 deselected；coverage 90.83%；9 个宿主保护路径零变化；9/9 Skill 源适配落地；Plan/Review/Write 核心模块均达到 90% 级覆盖；缺实现和 live gates 已逐项保留为未勾选；未 commit/push/tag/release，未上传 GitHub |
| 2026-08-09 | 目标收缩 | in progress | —（未提交） | 完整回读计划；核对工作树与 M5/M6 定向基线 | 174 passed | 当前完成定义改为本机 Windows Codex 可持续写小说；M7/M8 发布、跨平台与外部用户事项暂缓，M5/M6 安全与真实父任务裁决门保留 |
| 2026-08-09 | M5/M6 本机自动核心 | implementation complete / live install pending | —（未提交） | Plan/Write receipt 生产对抗；adapter/package/hygiene；collect/upstream-collect/full；Setup check；UTF-8/BOM/diff | pass | 1892 collected；1770 passed、15 skipped、107 deselected；coverage 90.41%；9 个宿主保护路径零变化；该轮停点为 5 个 Agent 待 Apply、personal marketplace 缺失与 Codex CLI Access denied，后续状态见下一行 |
| 2026-08-09 | Setup、0.3.0 与本机安装准备 | ready to push / App install pending | `3d08b3d`、`e9da1d8` | `codex-setup --apply/--check`；version/adapter/package/Plugin Creator/hygiene；隔离 `full`；manifest SHA 对比 | pass | 5 个 Agent 已创建并回读 current；personal marketplace 与本机插件源已建立；1893 collected、1771 passed、15 skipped、107 deselected、coverage 90.41%，9 个宿主路径零变化。用户因页面仍显示旧 `0.1.0` 暂停安装；仓库已提升到 `0.3.0`，待本轮授权 push 后刷新唯一 cachebuster 并重开安装页；不创建 tag/release |
| 2026-08-09 | 本机 Init live 与重复 rollout 修复 | parser fixed / cache refresh pending | —（未提交） | 9/9 Skill 真实发现；Setup check 5/5 current；初始化前 Doctor；Init dry-run；真实 Apply；重复 session/turn 对抗回归；adapter/package | targeted pass / live blocked | Init Apply 在旧安装版因同任务重复 `session_meta` fail-closed，项目目标保持不存在；已将安全合并规则统一到 Init/Plan/Review/Write/decision receipt，直接相关 6 文件 545 passed、10 skipped。新 cachebuster、App 重装与新顶层任务重跑仍待完成，故未关闭本机 live gate |
| 2026-08-09 | Desktop task-name / agent-path 绑定 | live source refreshed / App reinstall pending | —（未提交） | Write/Review/Init 与共享 rollout 对抗；独立只读安全审计；Skill Creator；adapter/package/version；UTF-8/BOM/diff；Plugin Creator cachebuster 与 live readback | 605 passed、11 skipped | canonical marker 派生完整 digest task name；严格 depth/path、final-only、top-level parent 与 receipt 全入口重验均 fail-closed。live source=`0.3.0+codex.20260809100531`，旧 source 已备份；WindowsApps 两个 CLI 入口均 Access denied，installed cache 仍为 `0.3.0+codex.local-20260809-060313`。独立 M3 smoke 只作模型/身份 smoke；待 App 内更新并新开顶层任务完成真实写作验收 |
| 2026-08-10 | 第 1 章、本地 RAG 与完整后续流程 | live flow complete / App reinstall pending | —（未提交） | default Write 真值审计；本地模型实际推理；projection retry；Doctor；独立 full Review；Query；Learn；Dashboard；定向与 `full`；adapter/package/Plugin Creator/Skill；Setup Apply/check；UTF-8/BOM/diff | 1849 passed、15 skipped | `write-ch0001-737f9df2a045` production complete；五个创作/审查阶段均为真实 Luna/medium；vector retry 14 条、五 projection done；Review `rv-ch0001-dde5084e0a50471b` 五维通过；本地 Qwen 默认与 GitHub README 已落地；Context H2 合同和安装形态 Review 导入已修复；source=`0.3.0+codex.20260809171729`、Setup 5/5 current，App cache 尚未更新；未 commit/push/tag/release |
| 2026-08-10 | 新缓存与新任务发现 | complete | —（未提交） | App cache manifest；宿主 Skill root；Plugin Creator validator；缓存 runtime `codex-setup --check`；工作区/缓存/validator 前后指纹 | pass | 独立新顶层任务 `019fe995-73ef-7031-a10a-2d243e2730bf` 从 `0.3.0+codex.20260809171729` 发现 9/9 Skill；5/5 Agent current、0 conflict、零写入；未 commit/push/tag/release |
| 2026-08-10 | 方案 B Init → Plan live gate | complete / two live deviations fixed | —（未提交） | 独立父任务真实 `Apply`；Init/Plan receipts；prewrite/project-status/Doctor；题材与状态定向回归；隔离 `full`；CSV/adapter/package/hygiene/UTF-8/diff | 1854 passed、15 skipped | 任务 `019fe9b5-abef-7e33-bcfe-76dec92cf922` 以 `gpt-5.6-sol/max`、`invoked_agents=[]` 完成 `F:\codexnovel\test.v.0.3` 的 Init 与 10 章 Plan，Doctor 0 blocker、无 Git。现场发现并修复“都市悬疑误路由都市赘婿流”和“未来合同把下一章推到第 10 章”；新 Init 回归生成悬疑推理合同，现有项目只读状态已回到第 1 章。原 smoke 项目的已提升合同保留为现场证据，未绕过 runtime 静默改写；未 tag/release。 |
| 2026-08-10 | 使用说明与 Luna/high | complete | 本次 main 提交 | Setup/runtime 两组定向测试；Codex adapter validator；UTF-8/BOM/diff | 121 passed、2 skipped；adapter 0 errors | 新增根目录《使用说明》，README 增加安装、本地 Qwen、Init/Setup/Plan/Write 入口；context/writer/reviewer/data 的新任务固定为 `gpt-5.6-luna / high`，deconstruction 与 Plan 继续继承父任务；升级前 Luna/medium 证据只作历史保留，旧签名 Review run 仍可兼容读取；不创建 tag/release。 |

### 默认假设

- 目标支持 Python 3.10–3.14；只有矩阵通过的版本才写入正式支持声明。
- Windows 是首要平台，Ubuntu 是正式辅助平台。
- Dashboard 的前端 dist 随插件发布，Node 只作为开发和 CI 构建依赖。
- 1.0 不引入 MCP Server 或 App Connector；本地 Python runtime 足以完成现有功能。
- 继续使用 GPL-3.0，保留上游作者、版本、SHA 和修改声明。
- Agent TOML 格式仍可能演进，因此每次 RC 都重新核对官方 Agent 文档。
