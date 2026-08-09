# 写章事务阶段

唯一顺序：

```text
preflight -> prewrite -> context_agent -> writer_draft -> reviewer
-> review_pipeline -> writer_final -> promotion -> data_agent -> precommit
-> commit -> projections -> postcommit -> backup -> complete
```

- `default`：reviewer 五维全部执行。
- `fast`：reviewer 执行 setting/timeline/continuity；character/logic 明确 skipped。
- `minimal`：生成本 run 新的 `no-review.json`，以两个 `minimal_mode` receipt
  跳过 reviewer/review_pipeline；context、writer、data 和所有 gate 仍执行。

`context_agent`、`writer_draft`、`reviewer`、`writer_final`、`data_agent` 只能由
`accept_verified_agent_stage` 推进。生产运行必须传入由 Codex 宿主轨迹解析出的
`VerifiedRuntimeEvidence`；TOML、模型自报或 canned fixture 均不能推进生产事务。
对外先使用 `prepare-agent --request-file` 固化输入 hash 和唯一 prompt marker，再使用
`accept-agent --request-file`：accept request 必须在本 run requests 目录，绑定绝对
rollout、launch request 与 payload 路径/hash；runtime 从受信 route/evidence 构造 envelope，
并要求 payload 与 marker 后唯一最终 assistant 输出精确一致。payload 不进入命令行。
request、launch、payload、manifest 与 review JSON 必须从同一次 stable bytes snapshot 同时
生成签名与解析值，禁止签名后按路径二读。每一阶段还必须消费本 run 的精确前序 lineage：
context 绑定 transaction，draft 绑定 context，reviewer 绑定 draft，final 绑定 draft+review
（minimal 为 fresh no-review），data 绑定 final+promotion target+review；任何旧 run artifact
或缺失 lineage 都阻断，context 之后的 downstream launch 也不接受额外无关 artifact。
writer manifest inputs 必须与对应 launch inputs 精确一致。
begin 必须显式接收当前 `workspace_root`，回读五个托管 Agent 的 current 状态并把 readiness
hash 绑定到 transaction；每次 agent acceptance 前重新检查，缺失、modified、conflict
或合同 hash 漂移均阻断。调用方自报的共享 parent ID 不能证明当前父任务身份；begin 必须
将非空 UUID `CODEX_THREAD_ID` 唯一绑定到可信 sessions 中的顶层父 rollout；带
`parent_thread_id` 或 `source.subagent` 的 session 拒绝。prepare-agent、accept-agent、
minimal-no-review、stage、promotion、complete 等每个可变 public 阶段都重验该路径/hash，
所有 child.parent 必须匹配，跨父任务 rollout 一律拒绝。

preflight/prewrite/review_pipeline/precommit/commit/projections/postcommit/backup/complete
只使用 `stage --request-file`。runtime 必须重跑 write gate，或回读精确章节 accepted
commit、最新五项 projection log、实际 Git tag/allowlist；request 中的布尔值或 map
不是事实证据。

每份 receipt 都包含序号、上一 receipt hash 和自身 hash。失败 receipt 不可改写；
修复后为同一阶段追加下一份 receipt。commit receipt 成功后，恢复只允许补
projections、postcommit 或 backup，不得重跑正文、审查或 data Agent。
minimal 的 reviewer/review_pipeline skip 使用同一 fresh no-review identity；若第一份
skip receipt 已写而第二份失败，重试只验证并续写缺失 receipt，不重复已完成 receipt。

writer 最终阶段只调用一次。clean review 只允许 `polish`；不要先写同名
`polished.md` 再以它为源重复 polish。blocking review 必须保留原审查并等待可信父任务的
有限选择及逐 blocking issue occurrence 的事务 resolution receipt。Writer v1 只兼容
`draft` / `polish`；Writer v2 result 与 manifest 必须携带完全相同的 `resolutions`，其中
`draft` / `polish` 为 `[]`，成功的 `targeted_fix` 按 `issue_index` + `issue_sha256` 逐项
声明 `resolved`。blocking review 先通过 `targeted-fix-request` 输出固定 choice request 与
binding marker，再由 `targeted-fix-decide` 从当前可信父 rollout 的第一条用户回答生成
scope-bound receipt。只有选择 `targeted_fix` 且 resolutions 精确覆盖原报告中每个 blocking
issue occurrence，事务才生成 resolution receipt 与 resolved review 并继续；原审查保持
不可变，缺失、重复、未知 index/hash、跨 scope 或篡改 receipt 均 fail-closed，不得二次 reviewer。

提升前检查：正文是否在本轮后手改、合同是否晚于正文、是否已有 accepted commit。
出现任一情况都报告冲突并停止。公开 `replace_with_verified` 字符串只是作用域 choice，
不是用户授权；runtime 使用 `recovery-request` / `recovery-decide` 绑定当前正文、终稿、
合同、accepted commit、run/transaction 与父任务 rollout，并只在 lifecycle lock 内重验
同一 scope 后接受 `replace_with_verified`。`keep_current` / `status_only` / `cancel` 零正文
写入。已有 accepted commit 时永远阻断，直到实现独立 amend transaction，避免正文与
commit/projection 分叉。最终 writer receipt 必须恰好一个 `polished` artifact，路径为本
run 的 `polished.md`；operation 为 clean review 的 `polish` 或完整 receipt 闭环的 `targeted_fix`。
可信选择 `keep_current` / `status_only` 后 status/resume 派生 `stopped`，选择 `cancel` 后
派生 `cancelled`；不伪造 promotion/complete receipt，若要改选必须启动新事务。
若正文已精确等于本 run 已验证 `polished.md`，且 accepted commit 不存在、promotion
receipt 缺失，runtime 只可将其识别为 owned recovery 并补同 run receipt；目标 bytes
不同、路径身份变化或新的 accepted commit 均不得借恢复路径覆盖。

Git backup 必须单独取得用户授权，marker 直接展示完整 HEAD 与 exact allowlist，并使用
scope challenge 和当前顶层父 rollout 中的一次性有限选择 receipt；canonical nonzero
`CODEX_THREAD_ID`、唯一路径、stable prefix、无重复/撤销选择缺一不可。公开 challenge/token
不能单独授权；strict registry 只有相同 scope/receipt 且实际 blob/tree/parent/message/tag
全部重验通过，才能从 retryable 状态恢复。只有根目录没有 `.git` 才记录 `skipped_non_git`；
probe error、gitfile/worktree/reparse/external objects/alternates/core.worktree/bare 均阻断。
项目级锁内清空继承 `GIT_*`、禁 hooks/fsmonitor/filter/`git add`，用 stable bytes、
项目内临时 index、`hash-object -w --no-filters`、`commit-tree` 构造快照，并用 `update-ref` CAS
发布与 readback tag；不得修改 HEAD、当前分支或普通 index。
complete 写入前及每次 status/resume 都重读 final artifact、正文、accepted commit、五项
projection、postcommit 和 backup 真源；任何一项缺失或 stale 时不得报告完成。
