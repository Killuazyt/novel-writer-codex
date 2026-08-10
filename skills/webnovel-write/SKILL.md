---
name: webnovel-write
description: 通过固定 Luna 项目 Agent 和不可变事务 receipt 起草、审查、润色、提升、提交并投影一个小说章节，支持 default、fast、minimal 及 commit 后断点恢复。用户说“写第 N 章”“继续写章”“快速/最简写章”或显式调用 $webnovel-write 时使用。
---

# Webnovel Chapter Workflow

主对话只编排、询问作者裁决并提升已验证 artifact；禁止自行补写或改写正文。运行时
模块不调用模型，必须由当前任务内的原生项目 Agent 产生结果，再用可信宿主 evidence
接受。无法取得 actual agent/model/effort 时立即阻断，不能用父模型接手。

## 模式

- `default`：完整 context、五维 reviewer、writer final polish 和全部 gate。
- `fast`：reviewer 只执行 setting/timeline/continuity，另两维明确 skipped。
- `minimal`：跳过 reviewer/anti-AI 深检，但生成本 run 新的 no-review artifact；writer
  仍做排版，context、data 和全部事务 gate 不可跳过。

## 按需参考

不要一次加载全部写作资料。起草或润色按场景读取：

- 通用去模板腔与润色顺序：
  [anti-ai-guide.md](references/anti-ai-guide.md)、
  [polish-guide.md](references/polish-guide.md)；
- 题材、语气与章节变体：
  [style-adapter.md](references/style-adapter.md)、
  [style-variants.md](references/style-variants.md)；
- 战斗、对话、情绪、场景、欲念、钩子或移动端排版：按触发读取
  [writing/](references/writing/) 中对应文件。

## 固定流程

严格执行 [transaction-stages.md](references/transaction-stages.md) 的顺序：

开始前先加载 [runtime-invocation.md](../../references/codex/runtime-invocation.md)。正文、审查
内容、用户回答与 Agent payload 只能进入受控 UTF-8 request/artifact 文件；argv 只传项目
根、run ID、枚举、数值和绝对 request-file 路径等可信标量。

1. `write-transaction begin --workspace-root <当前工作区>`，获取 run ID；begin
   必须确认工作区内固定 Agent 全为 current 并绑定管理 hash，随后运行 preflight、合同
   刷新与 prewrite。begin 还必须把非空 UUID `CODEX_THREAD_ID` 唯一定位到可信 sessions
   根下的当前顶层父 rollout 并绑定路径/hash；父 session 带 `parent_thread_id` 或
   `source.subagent` 时拒绝。每个会写状态的 public 阶段（prepare-agent、accept-agent、
   minimal-no-review、stage、promotion、complete）都重验当前父任务。每个 child.parent
   必须等于该 UUID，不得拿其他父任务或调用方自报 ID 代替。
2. 调用 `webnovel_context_agent`，只接收精简任务书结果。用 M3 runtime evidence 和
   payload gate 接受后，才交给 writer。先把明确输入 artifact 的绝对路径/hash 写入本
   run requests 目录，调用 `prepare-agent --request-file <ABS_JSON>`，把返回的精确 marker
   与 launch request 路径/hash 放入子 Agent prompt；同时必须把返回的
   `agent_task_name` 原样用作 `spawn_agent` 的 `task_name`，不得自行加前后缀、复用旧名称
   或根据角色另起别名。该 marker 派生的宿主路径必须精确为
   `/root/<agent_task_name>` 且 depth 为 1；这些值只由可信 rollout 回读，不能写进 accept
   request 自报。子任务只允许一个最终 assistant 输出。再把可信 rollout identity、
   launch request 与实际 payload 的绝对路径/hash 写入 accept request，调用
   `write-transaction accept-agent --run-id ... --request-file <ABS_JSON>`；正文和用户文本
   不得出现在 shell 参数。request、launch、payload、manifest 与 review JSON 的签名和
   解析必须来自同一次 stable bytes snapshot，禁止先签 A 再二读解析 B。
3. 调用 `webnovel_writer` 执行 `draft`。launch 必须消费本 run context receipt 绑定的
   payload；只接受本 run staging 中的 `draft.md`、manifest 与匹配 hash，manifest 的
   inputs 必须与 launch inputs 精确相同。
4. default/fast 调用 reviewer 一轮，且 launch 必须消费本 run draft；非法 JSON 只允许
   同 Agent/模型做一次序列化修复。minimal 运行 `minimal-no-review`，不得启动 reviewer
   或复用全局旧 artifact；两个 skip receipt 中途失败时只续写缺失 receipt，不重复已完成项。
5. 非 minimal 将可信 reviewer JSON 写入本 run staging，并以 staging-only 方式规范化且
   永久保留原报告。clean review 只允许后续 `polish`。blocking review 先运行
   `targeted-fix-request --run-id ...`，把返回的完整 binding marker 与 2–3 个有限选项原样
   显示给用户并等待回答；回答写入当前父任务 rollout 后，使用返回的绝对 request-file
   运行 `targeted-fix-decide`。只有同一 `CODEX_THREAD_ID` 父 rollout 中选择
   `targeted_fix` 的 scope-bound receipt 才能启动 final Writer；`report_only` / `abandon`
   不生成可提交正文。不得用调用方字符串声称“已定点修复”，也不得二次 reviewer。
6. clean review 再调用一次 writer final `polish`，输入必须精确绑定本 run draft 与
   normalized review（minimal 则绑定新 no-review），输出 `polished.md`；不要连续覆盖同名
   文件。Writer v1 只兼容 `draft` / `polish`，不得用于 `targeted_fix`；Writer v2 result 与
   manifest 必须精确包含同一份 `resolutions`。`draft` / `polish` 必须为 `[]`；成功的
   `targeted_fix` 必须按 occurrence index 与 issue SHA-256 返回逐项 resolved receipt。
   blocking 路径只在真实选择 targeted_fix 且每个 blocking issue occurrence 均有可信事务
   resolution evidence 时开放。runtime 要求 resolution 与原报告中的 index+hash 一一对应，
   生成不可变的事务级逐 issue resolution receipt 和 `review_results.resolved.json`；原审查
   永不改写，后续
   data/precommit/commit 只绑定 resolved review。
7. evidence、路径、hash、schema 全通过后，由 runtime 在 chapter lifecycle lock 内重验并
   原子提升到唯一 `正文/` 文件。作者手改、合同较新或已有 accepted commit 时阻断；裸
   `replace_with_verified` 字符串不是用户授权。有冲突时先运行
   `recovery-request --run-id ... --target ...`，显示完整 marker/有限选项并等待用户回答，再用
   `recovery-decide --request-file <ABS_JSON>` 从可信父 rollout 生成 receipt。只有选择
   `replace_with_verified` 且在 lifecycle lock 内重验正文、终稿、合同与 commit scope 未变，
   `promote --decision-receipt <ABS_JSON>` 才可覆盖；`keep_current` / `status_only` / `cancel`
   零正文写入。已有 accepted commit 永远不可覆盖。若目标
   已精确等于本 run 已验证 final artifact、没有 accepted commit 且仅缺 promotion receipt，
   runtime 将其识别为 owned recovery 并幂等补 receipt；任何不同 bytes 仍按作者冲突阻断。
8. 调用 data Agent；launch 必须同时绑定本 run final、promotion target 与本 run review。
   校验其三份固定 tmp artifact 后立即复制/绑定到本 run receipt，旧 artifact 不得进入 precommit。
9. 依次运行 precommit、chapter-commit、五项 projection、postcommit。commit 后失败
   只允许补 projection/postcommit/backup，不重跑任何 Agent。非 Agent 阶段通过
   `stage --request-file <ABS_JSON>` 推进；runtime 会重跑 gate 或回读精确 commit、最新
   projection 与 backup 真源，调用者自报 `gate_ok`/状态 map 不能推进。
10. 只有根目录确实没有 `.git` 时才记录 `skipped_non_git`；Git probe 超时、错误或畸形
    metadata 都阻断。Git backup 必须另问用户，并验证当前顶层父 rollout 中 scope-bound
    有限选择 receipt；`CODEX_THREAD_ID` 必须是 canonical nonzero UUID，重复/撤销选择拒绝。
    `--build-decision-receipt` 生成的 marker 直接展示完整 HEAD 与 exact allowlist，公开
    challenge/token 本身不是授权。禁止 Git init，只接受普通 standalone `.git` 实目录；拒 gitfile/worktree、
    reparse、external objects、alternates、`core.worktree` 与 bare repo。项目级锁内清空继承的
    `GIT_*`，禁 hooks/fsmonitor/filter 与 `git add`，以 stable bytes、`hash-object -w
    --no-filters`、项目内临时 index、`commit-tree` 构造快照，再以 `update-ref` CAS 发布并
    readback tag；HEAD 与普通 index 不变。strict registry 的 retry/completed 都重验实际
    blob/tree/parent/message/tag。真实 Git 写仍需 live smoke，未验证前不得自动完成 Git backup。
11. 所有 receipt 完整后，complete 与每次 status 都重读 final artifact、正文、accepted
    commit、五项 projection、postcommit 与 backup 真源；任一 stale/missing 都报告 stale，
    不记录或宣称完成。

静态行为案例位于 [evals.json](evals/evals.json)，只验证编排与 fail-closed 语义，
不能替代真实宿主模型路由 smoke。

## Agent 与 artifact gate

- context/writer/reviewer/data 必须实际为 `gpt-5.6-luna` / `high`；缺失、超时、合同
  过期、模型不符或 fallback 均阻断。
- Agent 自报、TOML 与 canned fixture 不是生产 evidence。canned 只允许 test-only 事务，
  其状态不得显示 production complete。
- 主对话只保留问题摘要、路径/hash、字数和状态；不回传整章正文或长审查内容。
- 当前 Desktop rollout 即使不保存明文 marker，也必须以 marker 独立派生的 exact
  `agent_task_name` / `/root/<agent_task_name>` 绑定；只接受唯一 `final` 或
  `final_answer` assistant 输出，忽略 commentary。旧 rollout 只有明确包含 exact marker
  时才进入独立兼容分支，不能与新 task-path 证据做宽松 OR。
- pending 消歧、遗漏必写节点、blocking reviewer 或 anti-AI blocker 不得越过 precommit。
- writer final receipt 必须唯一绑定本 run 的 `polished.md`。clean review 只允许
  `polish`；Writer v2 的 `resolutions` 必须再由事务 receipt 精确绑定原 blocking issue
  occurrence、Writer payload/manifest/rollout 与终稿，不能单独构成父任务授权。已有 accepted
  commit 的章节即使 receipt 选择 replace 也禁止覆盖；当前版本尚无 amend/rewrite 事务。
- 所有公开 challenge/token、CLI 枚举和调用方自报 parent ID 都只是作用域输入，不是
  用户授权或当前任务身份凭证；当前任务身份必须由 `CODEX_THREAD_ID` UUID 与唯一可信
  rollout 绑定，用户裁决仍只有可信宿主 rollout receipt 才能关闭对应 live gate。

## 恢复与最终报告

先运行 `write-transaction resume`。若 commit 已完成，严格按返回的
`retry_projection_only`、`run_postcommit_only` 或 `retry_backup_only` 继续。作者选择
保留当前正文、只看状态或取消时不得提升 artifact；只有本次父任务 rollout 的固定 request
和 scope-bound receipt 可授权替换，不得把 `--recovery-decision` 裸字符串当作授权。
`keep_current` / `status_only` 派生 `stopped`，`cancel` 派生 `cancelled`；两者都不追加
promotion receipt、`production_complete=false`，如需改选必须开始新事务。每次 resume/status
都必须重新执行 receipt 与 current-truth audit，而不是只看旧 receipt。

最终报告列出模式、正文路径/hash/字数、Agent evidence 状态、review/data/gate、commit、
五项 projection 与 backup receipt。任何必需项缺失时使用“未完成/待恢复”，禁止写
“已完成”。
