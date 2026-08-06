# novel-writer-codex 完整移植实施计划

## 1. 项目目标与当前基线

计划文件落盘位置：

`F:\codexnovel\novel-writer-codex\docs\IMPLEMENTATION_PLAN.md`

本文件已于 2026-08-06 以 UTF-8 无 BOM 回读校验，并继续作为唯一详细实施清单；[PORTING.md](PORTING.md) 只保留简要迁移状态。本轮实施止于 M1，不进入 M2。

### 最终目标

把上游 `lingfengQAQ/webnovel-writer` 移植为 Codex 专用插件，在不迁移小说数据、不改变 Story System 业务契约的前提下，实现：

- 9 个 Codex Skill：保留 8 个 `$webnovel-*` 名称，新增 `$webnovel-setup`。
- 4 个项目级 Codex Agent。
- 原有初始化、规划、查询、学习、审查、写章和 Dashboard 功能。
- Windows 中文路径完整支持。
- GitHub 仓库及 Repo Marketplace 分发。
- 独立 SemVer、CI、上游漂移检查和可重复发布流程。

### 已锁定决策

- 只维护 Codex 下游，不继续维护 Claude Code 主路径。
- `.claude` 配置只允许兼容读取，任何新流程都不得写入。
- `.story-system`、`.webnovel`、`正文`、`设定集`、`大纲`保持完全兼容，不做数据迁移。
- 专用 Agent 采用项目级 `.codex/agents/*.toml`，由 `$webnovel-setup` 显式安装。
- Agent 缺失或版本不匹配时阻断相关 Skill，不静默降级为主 Agent 模拟。
- `$webnovel-init` 默认不初始化 Git，必须询问用户。
- `$webnovel-review` 先交付单章，1.0 前补齐一次最多 5 章的串行范围审查。
- 文档以中文为主，命令、文件名、Schema 字段保持英文。
- 每个里程碑验收后先汇报，经用户确认再提交；不自动 push、tag 或发布。

### 2026-08-06 基线

| 项目 | 当前状态 |
|---|---|
| Git | `main` 已初始化，`origin` 已配置，尚无提交 |
| 上游 | `master@2041abad78211e29a67a2f0c64b2a97a747dce57`，版本标记 `6.2.1` |
| 文件 | 目标 346 个有效文件；320 个与上游同哈希，10 个已适配，62 个待迁，16 个 Codex 新增 |
| Skills | `0/9` |
| Agents | `0/4` |
| 测试 | 收集 746 项；当前全量套件含 Claude 契约且可能访问真实 `~/.claude`，不能直接作为 CI |
| 已通过 | Codex Adapter、Plugin manifest、Hook 单测、核心 commit/projection/write-gate 定向测试 |
| 发布能力 | 尚无 CI、Marketplace、release note、上游 lock 或真实安装 smoke |

官方约束以当前的 [插件打包](https://developers.openai.com/plugins/build/plugins)、[Skill](https://learn.chatgpt.com/docs/build-skills)、[项目级 Agent](https://learn.chatgpt.com/docs/agent-configuration/subagents)和 [Hooks](https://learn.chatgpt.com/docs/hooks)文档为准。

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

其中 M1 已完成 `WEBNOVEL_HOME`、native registry 与 `.env` 优先级；工作区 pointer 的完整 Codex 化仍属于 M2，M1 不越界迁移。

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
  - `webnovel_reviewer`
  - `webnovel_data_agent`
  - `webnovel_deconstruction_agent`

- Agent 角色合同以 `references/agents/*.md` 为唯一语义真源，由生成器产生 `.codex/agents/*.toml`。
- TOML 不固定模型和 reasoning effort，继承父会话。
- context、reviewer、deconstruction 使用 `read-only`；data 使用 `workspace-write`，但只允许生成三份 `.webnovel/tmp` artifact。
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
- `write default/fast` 必须发现 context、reviewer、data。
- `write minimal` 仍必须发现 context、data，只跳过 reviewer。
- `init` 无参考作品时可独立运行；提供参考文本时必须发现 deconstruction。
- 缺失或哈希过期时统一阻断，并引导 `$webnovel-setup` → 新任务；禁止兼容模式冒充专用 Agent。

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

- [ ] 完成工作区 pointer 的 Codex 化和其余 runtime 宿主中立化。
- [ ] 所有剩余 pointer 与 runtime 状态改写 `.codex`/`WEBNOVEL_HOME`；native registry 已在 M1 完成。
- [ ] `.claude`、`CLAUDE_*` 仅保留只读 fallback，并在结果中标记 `legacy_read_only`。
- [ ] Skills 统一通过自身文件位置推导插件根，不依赖 `PLUGIN_ROOT`；该环境变量只在 hooks 中使用。
- [ ] 修复版本、package、release validator 中的 `.claude-plugin`、嵌套仓库路径和旧插件名硬编码。
- [ ] 建立 `upstream_sync.py` 及 lock 漂移测试。
- [ ] 把 Bash-only 语法、Claude slash command、Claude 工具名纳入静态扫描。

验收：

- Windows 中文、空格、括号、`&` 和跨盘路径通过。
- 显式 `--project-root` 不会被旧 registry 覆盖。
- 不会误命中上一本书、插件缓存目录或父仓库。
- 旧项目无需迁移即可读取，新运行只写 Codex 路径。
- runtime `scripts/data_modules` 覆盖率保持不低于 90%。

建议提交：

- `refactor(runtime): add host-neutral path and config resolution`
- `test(packaging): add Codex package and upstream drift validators`

### M3：Skill 公共框架、Setup 与四个 Agent

任务：

- [ ] 新增 `$webnovel-setup`。
- [ ] 为每个 Skill 保留仅含 `name`、`description` 的 frontmatter。
- [ ] 为每个 Skill 增加 `agents/openai.yaml`，default prompt 显式使用 `$skill-name`。
- [ ] 建立共享 Codex runtime 调用说明，去除 `export`、`$PWD`、`$()`、`cat/test/find/seq/printf`、`/dev/null` 及 shell 循环。
- [ ] 移植四个 Agent 合同并生成项目 TOML。
- [ ] 加入 TOML/合同哈希漂移校验和 Setup 幂等测试。
- [ ] 验证 Hooks 在未信任时被跳过、信任后触发；runtime 在两种情况下都安全。

Agent 验收：

- context：只返回完整任务书，零写入，缺少事实时返回 blocker。
- reviewer：严格 JSON、五维字段齐全、无评分；非法 JSON 最多重试一次，之后阻断。
- data：只生成 `fulfillment_result.json`、`disambiguation_result.json`、`extraction_result.json`。
- deconstruction：书名但无可靠正文时必须 `quality.passed=false`，不得编造或创建 canon。
- 四个 Agent 都通过伪系统提示、忽略指令等 prompt-injection fixture。
- TOML sandbox 不能作为唯一保护；runtime、路径校验和前后哈希共同构成边界。

建议提交：

- `feat(setup): add explicit Codex project agent provisioning`
- `feat(agents): add canonical Codex agent contracts`

### M4：Read-only Alpha，目标版本 0.1.0

迁移：

- [ ] `$webnovel-doctor`
- [ ] `$webnovel-query`
- [ ] `$webnovel-dashboard`

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

- 三个 Skill 在真实安装后的新 Codex 任务中可发现。
- Windows 中文路径 smoke 通过。
- Hooks 未信任/已信任两种状态均验证。
- 达标后才允许在用户授权下创建 `v0.1.0`。

建议提交：

- `feat(skills): port doctor and query`
- `feat(dashboard): add safe lifecycle workflow`

### M5：Controlled-write Beta，目标版本 0.2.0

迁移：

- [ ] `$webnovel-learn`
- [ ] `$webnovel-review` 单章版
- [ ] `$webnovel-init`
- [ ] `$webnovel-plan`

Learn 验收：

- 用户文本通过 JSON 输入，不进入 shell 拼接。
- 首次写入、追加、完全重复跳过、损坏 JSON 拒绝均正确。
- 保留旧记录、锁、原子替换和 UTF-8。
- 新经验能被后续 `memory-contract load-context` 实际读取。

Review 验收：

- reviewer 严格输出 setting、timeline、continuity、character、logic 五维结果。
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

- 输出卷节拍表、卷时间线、详细大纲和总纲写回。
- 每章具有时间字段、1 个 CBN、2–4 个 CPN、1 个 CEN 及最多 4 个必须覆盖节点。
- 时间单调、倒计时正确、相邻 `CEN → CBN` 承接。
- 规划范围内每章均生成 volume/chapter/review runtime contract。
- blocker 未裁决或 `plan-validate` 失败时，不更新 state 和 Story System。
- 批次失败只重做失败批次，不覆盖已通过结果。
- 目标首章最终通过 `write-gate --stage prewrite`。

建议提交：

- `feat(learn): port safe project learning`
- `feat(review): port structured single-chapter review`
- `feat(init): port confirmed project initialization`
- `feat(plan): port validated volume planning`

### M6：Full-write Beta，目标版本 0.3.0

迁移：

- [ ] `$webnovel-write`
- [ ] `$webnovel-review` 范围审查
- [ ] 全事务故障恢复

写章顺序固定，不允许并步：

```text
preflight
→ contract refresh / prewrite
→ context agent
→ draft
→ reviewer
→ review-pipeline
→ polish
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

建议提交：

- `feat(write): port transactional chapter workflow`
- `feat(review): add resumable serial range review`
- `test(write): add failure injection and resume coverage`

### M7：Release Candidate，目标版本 0.9.0

任务：

- [ ] 建立 GitHub Actions。
- [ ] Codex 化 version、package、release-note validator。
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

### M8：Stable 1.0

完成条件：

- 9 个 Skill 全部可发现，显式 `$skill` 和自然语言触发均通过。
- 4 个项目 Agent 均通过 Setup、更新、冲突和新任务发现测试。
- 旧小说项目无需数据迁移即可打开和继续写作。
- 主路径不再依赖 Claude 配置；兼容层从不写 `.claude`。
- default/fast/minimal、单章/范围审查及失败恢复全部通过。
- Windows 中文路径和 Ubuntu 回归通过。
- Hooks 未信任时不误报保护已启用；信任后真实 deny 测试通过。
- clean tag 构建的 archive 不含密钥、用户状态、小说数据、缓存或 coverage。
- manifest、CHANGELOG、release note、Marketplace tag、UPSTREAM lock 完全一致。
- 从 Git Repo Marketplace 安装到全新缓存后，在新 Codex 任务完成 Setup → Init/打开旧书 → Plan → Review → Write smoke。
- 用户显式批准后才创建 `v1.0.0`、推送和发布。

## 4. 测试、CI 与故障矩阵

### 自动测试层级

1. 静态校验：JSON/TOML/YAML、manifest、Skill frontmatter、Agent hash、UTF-8 无 BOM、Claude-only/Bash-only 扫描。
2. 单元测试：路径解析、Setup、Hooks、Schema、锁、原子写、投影、validator。
3. 行为测试：使用 canned Agent 输出，不在 CI 调用真实模型。
4. 集成测试：新项目、旧 Claude 配置项目、非 Git 项目、嵌套父仓库。
5. 故障注入：reviewer 非 JSON、artifact 缺失、SQLite busy、projection 中断、backup 中断、并发重复提交。
6. 人工发布 smoke：真实 Codex 安装、Hook 信任、Setup、新任务及项目 Agent。

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

### 默认假设

- 目标支持 Python 3.10–3.14；只有矩阵通过的版本才写入正式支持声明。
- Windows 是首要平台，Ubuntu 是正式辅助平台。
- Dashboard 的前端 dist 随插件发布，Node 只作为开发和 CI 构建依赖。
- 1.0 不引入 MCP Server 或 App Connector；本地 Python runtime 足以完成现有功能。
- 继续使用 GPL-3.0，保留上游作者、版本、SHA 和修改声明。
- Agent TOML 格式仍可能演进，因此每次 RC 都重新核对官方 Agent 文档。
