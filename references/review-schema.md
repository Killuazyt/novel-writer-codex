# Review 严格输出与持久化合同

> **适用入口**：`$webnovel-review` 单章与最多五章的串行范围审查
>
> **运行时真源**：`scripts/data_modules/review_schema.py`、`review_workflow.py`
>
> **原则**：Reviewer 只产生事实审查 JSON；运行时独立验证 Agent 身份、输入 hash、严格 schema、持久化 provenance 与数据库回读。

## Reviewer 顶层对象

Reviewer 必须只返回一个 JSON object。顶层字段必须且只能是以下七个字段，不得增加 wrapper、评分、日志、解释或未知字段：

```json
{
  "chapter": 12,
  "issues": [
    {
      "severity": "critical",
      "category": "timeline",
      "location": "第3段",
      "description": "倒计时与可信上下文冲突",
      "evidence": "正文写三日，可信记录为一日",
      "fix_hint": "仅修正倒计时数字",
      "blocking": true
    }
  ],
  "issues_count": 1,
  "blocking_count": 1,
  "has_blocking": true,
  "dimension_results": [
    {"dimension": "setting", "conclusion": "pass"},
    {"dimension": "timeline", "conclusion": "发现1个问题：倒计时冲突"},
    {"dimension": "continuity", "conclusion": "pass"},
    {"dimension": "character", "conclusion": "pass"},
    {"dimension": "logic", "conclusion": "pass"}
  ],
  "summary": "1个问题：1个阻断"
}
```

类型与一致性要求：

- `chapter` 必须是正整数，且与 prepare 阶段锁定的章节完全相同；布尔值不视为整数。
- `issues` 必须是数组，最多 200 项。
- `issues_count`、`blocking_count` 必须是整数；`has_blocking` 必须是布尔值。三者必须由 `issues` 精确推导并相互一致。
- `dimension_results` 必须是恰好五项的数组。
- `summary` 必须是非空字符串。
- 整个 reviewer JSON 的 UTF-8 编码不得超过 512 KiB。
- 顶层、issue 和 dimension object 均禁止额外字段；类型不符、缺字段、计数不符或未知字段一律为 `invalid_reviewer_json`。

## Issue 的七字段合同

每个 issue 必须且只能包含以下七个字段，全部必填：

| 字段 | 严格类型与取值 |
|---|---|
| `severity` | string；只能是 `critical`、`high`、`medium`、`low` |
| `category` | string；只能是 `setting`、`timeline`、`continuity`、`character`、`logic` |
| `location` | 非空 string |
| `description` | 非空 string |
| `evidence` | 非空 string，必须给出可核验的最短证据 |
| `fix_hint` | 非空 string，必须给出不改变规划目标的定点修复方向 |
| `blocking` | bool |

所有字符串都拒绝 NUL，并受运行时长度上限约束。`severity="critical"` 必须同时满足 `blocking=true`；否则整个响应无效。其他严重度也只有在事实链或提交安全确实被破坏时才能设置 `blocking=true`。

## 固定五维与 full/fast

`dimension_results` 必须按以下固定顺序完整出现：

1. `setting`
2. `timeline`
3. `continuity`
4. `character`
5. `logic`

每项必须且只能包含 `dimension` 与非空 string `conclusion`。

- `full`：五维都必须实际检查，任何以 `skipped` 开头的 conclusion 都无效。
- `fast`：仍输出完整五维；前三维实际检查，`character` 与 `logic` 的 conclusion 必须精确等于 `skipped: fast mode`，并且 `issues` 不得包含这两个跳过维度。
- 某维没有 issue 时 conclusion 必须精确等于 `pass`。
- 某维存在 issue 时 conclusion 不得为 `pass`。
- 维度缺失、重复、乱序或加入第六维都无效。

## Runtime identity 与 run provenance

Reviewer JSON 自报的模型信息不构成证据。accept 阶段必须验证显式 Codex rollout，并将以下信息绑定到 run ledger：

- `run_id`、`range_id`、`review_mode` 与章节号；
- prepare 时宿主提供的规范 UUID `CODEX_THREAD_ID`；accept、裁决与范围推进必须仍处于同一父任务；
- `agent_name=webnovel_reviewer`；
- requested/actual model 与 reasoning effort；
- rollout evidence source、evidence SHA-256、child/parent task IDs；
- 正文章节、只读 context、request 与原始 reviewer 输出的路径和 SHA-256；
- 每次响应的顺序、schema 状态与输出 SHA-256，最多一次同 Agent、同 route 的序列化重试。

实际身份必须是托管合同指定的 `gpt-5.6-luna` / `medium`。缺少 rollout、合同 hash 漂移、父模型回退、输入变化或身份不一致均必须阻断，不能由父 Agent 修补 JSON。

通过验证后生成不可变、run-specific 的 `webnovel-review-artifact/v1`。该 artifact 除上述七个业务字段外，必须包含并校验：

- `run_id`、`range_id`、`review_mode`；
- `chapter_sha256`、`context_sha256`、`reviewer_output_sha256`。

恢复时必须重新核对 artifact 路径、hash、schema version 和这些 provenance 字段；不一致时不得写报告或数据库。

## 裁决、持久化与数据库一致性

存在 blocking issue 时，运行时必须先停在 scoped 用户裁决；用户未选择 `report_only` 前不得生成公共报告、metrics 或数据库行。裁决必须返回固定有限选项与精确 binding marker，由当前父任务在 assistant 消息中原样展示；生产 CLI 只接受不含 `choice`/`answer` 的 `webnovel-review-decision-request/v1`，并从可信父任务 rollout 中提取 marker 后第一条真实 user 回答。该 rollout 的 thread ID 必须同时等于 prepare 时记录的宿主 `CODEX_THREAD_ID` 和绑定 Reviewer 子 Agent 运行证据中的 parent thread ID；仅位于可信 sessions 根但属于另一顶层任务仍不授权。裸 choice 参数、Agent 转述、旧 receipt、跨 run/range receipt、子 Agent rollout 或重放均不授权任何分支。`targeted_fix` 不授权 Review 修改正文，`abandon` 不产生公共持久化结果。

允许持久化后，顺序固定为：

1. 校验不可变 reviewer artifact 与输入 hash；
2. 原子写入 run-specific `review_metrics.json`；
3. 原子写入唯一、带 `run_id` 的审查报告；
4. 写入 `index.db.review_metrics`；
5. 只读回读数据库并核对 `overall_score`、`dimension_scores`、`severity_counts`、`critical_issues`、`report_file`、`notes` 与生成的 metrics 完全一致。

Reviewer 不提供或覆盖上述数据库投影列；它们只能由已验证 issue artifact 确定性生成。`review_metrics.json` 的 provenance 必须记录 `run_id`、`range_id`、mode、正文/context/reviewer hash、actual model 与 effort；数据库 `notes` 必须至少绑定 run、reviewer 输出和正文 hash。

数据库写入失败或 readback 不一致时，run 保持 recoverable，只恢复缺失的 artifact/数据库阶段并返回 `reviewer_rerun=false`。DB+WAL 但无 SHM 时只能在路径、WAL header/frame/salt/checksum 与大小边界全部通过后交给 SQLite 受控 checkpoint 和 integrity check；其他 sidecar 组合或损坏 WAL 必须结构化阻断。恢复不得重新调用 Reviewer，也不得重复修改正文。

每次返回 persisted 或范围 completed/partial/stopped 前必须重新核对 run-specific raw/result/metrics/report 的精确路径与 hash、确定性渲染内容和数据库 readback；缺失项只能续做持久化，篡改或冲突不得被覆盖。范围审查推进下一章前还必须回验既有 stop/continue receipt；若当前章节或 context hash 已变化，当前 run 必须标记为 stale 并等待范围裁决。
