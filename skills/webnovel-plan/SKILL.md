---
name: webnovel-plan
description: 在当前 Codex 主对话中为既有小说规划指定卷，生成并确定性校验卷节拍表、卷时间线、详细章纲和总纲写回，再安全提升并刷新写作合同。用户说“规划第 N 卷”“拆章纲”“生成卷纲/时间线”或显式调用 $webnovel-plan 时使用。
---

# Webnovel Plan

只在当前主对话规划。继承当前任务模型，不调用 context、writer、reviewer 或 data
Agent 代替规划。任何冲突都由主对话提供 2–3 个有限选项并等待用户回答。

## 固定流程

开始前先加载 [runtime-invocation.md](../../references/codex/runtime-invocation.md)。所有
自由文本、规划正文与用户回答只写入受控 UTF-8 request/artifact 文件；argv 只传项目根、
run ID、枚举、数值和绝对 request-file 路径等可信标量。

1. 运行只读 preflight、project-status、doctor，并执行带 `--save` 的 `plan-request`，原子
   写出固定 `.webnovel/tmp/plan-runs/<run_id>/plan-request.json`。确认目标卷、精确章节
   范围与每批 8–12 章；默认 10 章。若总纲缺少卷名、核心冲突或卷末高潮，先阻断。
2. 按需读取根目录的卷节拍/时间线模板、当前题材 profile、strand weave 和相关
   outlining reference。不要一次加载全部参考。卷结构、冲突、章纲、题材节奏或框架
   分别查阅 [outline-structure.md](references/outlining/outline-structure.md)、
   [conflict-design.md](references/outlining/conflict-design.md)、
   [chapter-planning.md](references/outlining/chapter-planning.md)、
   [genre-volume-pacing.md](references/outlining/genre-volume-pacing.md) 与
   [plot-frameworks.md](references/outlining/plot-frameworks.md)。
3. 只写 `.webnovel/tmp/plan-runs/<run_id>/`。严格按 `plan-request.json` 的 `batches`
   顺序规划；每批把完整章节对象写入固定
   `batches/batch-<start:06d>-<end:06d>.json`，结构与命名见
   [manifest-schema.md](references/manifest-schema.md)。规划正文只进入 fragment 文件，
   不进入 argv。
4. 每写完一批，立即运行 `plan-transaction accept-batch --request-file <ABS_REQUEST_JSON>
   --fragment-file <ABS_FRAGMENT_JSON>`。只有 `accepted` receipt 才表示该批通过；已接受
   fragment 的任一字节都不可修改。accept 失败且没有 receipt 时只重做该未接受批；若已
   接受批需要改动，废弃整个 run 并新建 request，不得删除/伪造 receipt 绕过。
5. 全部 request batches 均已接受后，按 accepted fragments 的原章节对象、原顺序无损组装
   `chapters`，再生成卷节拍表、卷时间线、卷详细大纲、总纲写回 JSON 和唯一固定的
   `plan-manifest.json`。完整 schema 与机器标记见
   [manifest-schema.md](references/manifest-schema.md)。
   marker 会重读全部 accepted receipts，要求范围无重叠且完整覆盖，并要求 fragment
   章节与最终 manifest 逐对象相等；任一缺失、篡改或旧 run receipt 都 fail-closed。
6. 先运行 `plan-transaction marker --manifest ... --request-file <ABS_JSON>`。当前父任务必须
   把返回的唯一 marker 作为真实 commentary 输出写入自身可信 rollout；随后在固定 run
   目录写 `parent-evidence.json`，指向可信 Codex sessions 根下的当前父 rollout/thread。
   再运行 `plan-transaction validate --manifest ... --request-file <ABS_JSON>
   --parent-evidence-file <ABS_JSON>`。无法从活跃父 rollout 解析 model/effort 或 marker 时，
   或 `CODEX_THREAD_ID` 缺失/不是 UUID/不能唯一定位同一 rollout 时，明确报告 live gate
   pending，不得自报身份或改用旧 rollout。
7. validation receipt 成功后运行 `plan-transaction apply`；apply 会重算 manifest、request、
   parent evidence 与所有 artifact hash。若退出码为 1 且状态为 `choice_required`，读取返回的
   `choice_request`，把返回的 `binding_marker` 作为一整行原样输出，再向用户提供“保留现有 /
   替换为已验证规划 / 取消本次规划”三个有限选项并等待回答；marker 后不得在用户回答前输出
   另一条持久化 assistant 消息。回答只留在当前父任务 rollout，不放 argv 或自行写入 receipt。
   随后运行 `plan-transaction decision --request-file <ABS_DECISION_REQUEST_JSON>`，再把生成的
   绝对 receipt 路径传给 `apply --decision-receipt <ABS_DECISION_RECEIPT_JSON>`。只有 `replace`
   才能覆盖该 request 中 before/after hash 精确绑定的冲突；`keep`/`cancel` 返回零事实写结果。
8. 按顺序执行 master-outline、state、全部章节 Story System 合同刷新，并为每步写 downstream
   receipt。任一步遇到 authored conflict 时重复第 7 步的 request/marker/decision 流程，再用
   `stage --decision-receipt <ABS_DECISION_RECEIPT_JSON>` 重试同一步；不得跨 stage/run 复用回执。
   某步失败只重试该步。
9. 对目标首章运行 `write-gate --stage prewrite`。只有四个 downstream stage 与
   prewrite 全部完成才能报告规划完成。

## 硬边界

- `executor` 必须为 `parent`、`invoked_agents` 必须为空、`planning_model` 必须等于
  当前父模型。静态描述不能替代真实宿主 smoke。
- 规划 manifest 用数值时间、数值倒计时和 handoff ID 表达可验证关系；不要让脚本
  从自由中文猜测语义。
- 章级设计遵守 `CEN→CBN`：先明确本章事件节点，再写事件如何改变角色/关系/资源状态；
  具体约束仍以按需加载的章纲 reference 为准。
- parent-only/model 继承必须由当前父 rollout 证据绑定 request/manifest/artifact hash；静态
  Skill 文案、调用方字段或其他父任务 rollout 均不构成证明。receipt 的 thread 必须等于
  非空 UUID `CODEX_THREAD_ID`，且在可信 sessions 根唯一匹配。
- `plan-validate` 失败时零小说事实写入。apply 中断必须恢复所有原目标。
- batch acceptance 只接受 request 指定的固定路径、精确范围与完整章节序列；accepted
  receipt 绑定 run、request hash、fragment 原字节 hash。缺批、重叠、旧 receipt、空批或
  accepted fragment 被改动时，不得生成 marker 或进入 validate。
- 设置冲突、覆盖已有规划和卷末钩子取舍必须等待用户。decision request/receipt 必须绑定
  validation、run、stage、冲突文件 before/after hash、当前 `CODEX_THREAD_ID`、父 model/effort
  以及 marker 到回答为止的可信 rollout prefix；任一不再匹配都 fail-closed。公开 scope
  challenge、裸 `--overwrite-token`、其他父任务的回答或调用方自造 JSON 均不构成授权。
- apply 与 downstream receipt 都绑定实际使用的 decision receipt 路径/hash；status/replay
  会重新验证当前事实、固定 request 和同父 rollout prefix，不能用旧成功 receipt 掩盖漂移。
- 全部文本使用 UTF-8 无 BOM；路径必须留在当前小说项目。

## 恢复与汇报

重跑时先查 transaction status，并逐一回读 request batches 的 accepted receipt。只重做
没有 accepted receipt 的失败批；已接受批保持原字节。apply 后从第一个未完成的
master_outline/state/contracts/prewrite stage 继续。若状态回到 `choice_required`，只使用本次
返回的固定 request 文件重新收集决定；不要沿用旧 scope challenge 或旧 decision receipt。
最终仅汇报 fragment/receipt 状态、artifact 路径/hash、validator 结果与下一步；不要把整卷
章纲复制进主对话。
