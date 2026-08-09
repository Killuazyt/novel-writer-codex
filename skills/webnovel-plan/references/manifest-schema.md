# 规划 manifest 合同

规划内容先写到 `.webnovel/tmp/plan-runs/<run_id>/`，不得直接覆盖 `大纲/`。
`plan-manifest.json` 使用 `webnovel-plan-manifest/v1`，核心字段如下：

```json
{
  "schema_version": "webnovel-plan-manifest/v1",
  "run_id": "plan-v1-example",
  "executor": "parent",
  "parent_model": "当前主对话模型",
  "invoked_agents": [],
  "volume": 1,
  "chapter_range": [1, 10],
  "content_sha256": "64位小写hex",
  "blockers": [],
  "artifacts": {
    "beat": {"path": ".webnovel/tmp/plan-runs/.../第1卷-节拍表.md", "target": "大纲/第1卷-节拍表.md", "sha256": "..."},
    "timeline": {"path": ".webnovel/tmp/plan-runs/.../第1卷-时间线.md", "target": "大纲/第1卷-时间线.md", "sha256": "..."},
    "outline": {"path": ".webnovel/tmp/plan-runs/.../第1卷-详细大纲.md", "target": "大纲/第1卷-详细大纲.md", "sha256": "..."},
    "writeback": {"path": ".webnovel/tmp/plan-runs/.../第1卷-总纲写回.json", "target": "大纲/第1卷-总纲写回.json", "sha256": "..."}
  },
  "beat": {
    "crises": [{"conflict": "...", "cost": "...", "result": "..."}],
    "midpoint": {"event": "...", "reason_if_none": ""},
    "final_open_question": "..."
  },
  "chapters": []
}
```

每章对象必须提供：`chapter`、`goal`、`time_offset_minutes`、`span_minutes`、
`transition`、`time_mode`、`countdowns`、`cbn`、`cpns`、`cen`、
`must_cover_nodes`、`forbidden_zones`、`chapter_end_open_question`。

节点是 `subject/action/result/handoff_id` 对象。每章恰好 1 个 CBN、2–4 个
CPN、1 个 CEN；上一章 CEN 与下一章 CBN 的 `handoff_id` 必须相同。
`must_cover_nodes` 最多 4 个，`forbidden_zones` 最多 5 个。

`countdowns` 是 `{事件名: 剩余分钟}`。相邻章同一事件的剩余分钟必须按
`time_offset_minutes` 的差值递减。闪回仍使用当前叙事锚点，并将
`time_mode` 设为 `flashback`、填写 `flashback_note`。

三个 Markdown 文件首部必须包含：

```text
<!-- webnovel-plan-content-sha256: <content_sha256> -->
```

总纲写回 JSON 必须包含同值 `plan_content_sha256`。详细大纲应逐字包含每章
目标、节点三元组和章末未闭合问题；时间线应包含 `第N章`、`T+<分钟>m`，
以及每个倒计时的 `CD:<事件>=<剩余分钟>m`。这些机器标记只用于确定性校验。

## 批次 fragment 与 accepted receipt

`plan-request.json` 的每个 `{start_chapter, end_chapter}` 必须对应唯一固定文件：

```text
.webnovel/tmp/plan-runs/<run_id>/batches/batch-<start:06d>-<end:06d>.json
```

fragment 只允许以下字段；`chapters` 必须按章号递增并精确覆盖该批一次：

```json
{
  "schema_version": "webnovel-plan-batch-fragment/v1",
  "run_id": "plan-v1-example",
  "volume": 1,
  "start_chapter": 1,
  "end_chapter": 10,
  "chapters": []
}
```

每批写完后用 `plan-transaction accept-batch --request-file <ABS_REQUEST_JSON>
--fragment-file <ABS_FRAGMENT_JSON>` 验收。receipt 固定写入
`.webnovel/plan-runs/<run_id>/batches/batch-<start:06d>-<end:06d>.accepted.json`，绑定
project/run/volume、request 固定路径与 hash、fragment 固定路径与原字节 hash。receipt
一旦 accepted 即不可替换；该 fragment 后续任何字节变化都会阻断整个 run。accept 失败且
未产生 receipt 时，只重做该未接受批。

最终 manifest 的 `chapters` 必须由全部 accepted fragment 原对象按 request 顺序组装。
marker/validation 会重读每份 receipt 与 fragment，检查批次无重叠、完整覆盖 request 范围，
并检查 fragment 与 manifest 对应章节逐对象相等；缺批、空批、旧 run receipt 或篡改均
fail-closed。规划正文不得放入命令行参数。

## 父任务证据与冲突边界

`plan-request --save` 必须先写出固定 `plan-request.json`。当前父任务随后输出
`plan-transaction marker` 生成的精确 marker；固定 `parent-evidence.json` 只能指向可信
Codex sessions 根下的当前父 rollout，并绑定 request、manifest、四份 artifact 与父任务
model/effort。thread 必须等于非空 UUID `CODEX_THREAD_ID`，并在 sessions 根唯一定位该
rollout；无法机器解析时状态是 live gate pending，不得手填通过。

apply、master outline、state 或 contracts 遇到既有不同事实时，以退出码 1 返回
`choice_required`、固定 `decision_request_file`、有限 `choice_request` 与精确
`binding_marker`。父任务把 marker 作为一整行原样输出并等待下一条持久化用户回答，再调用
`plan-transaction decision --request-file <ABS_DECISION_REQUEST_JSON>`；用户回答不得进入 argv，
也不得由调用方写成 JSON 冒充 receipt。生成的 receipt 再经 `apply` 或 `stage` 的
`--decision-receipt <ABS_DECISION_RECEIPT_JSON>` 使用。

request scope 精确绑定 validation/run/stage、每个冲突目标的 before/after hash、当前
`CODEX_THREAD_ID` 与父 model/effort；receipt 另绑定同一父 rollout 中唯一 marker、紧随其后的
有限选择回答及截至回答的 prefix hash。只有 `replace` 允许锁内重验后覆盖，`keep`/`cancel`
零小说事实写入。公开 scope challenge、裸 `--overwrite-token`、跨 run/stage receipt、自造
receipt、改变后的目标或不匹配的 rollout prefix 均 fail-closed。apply/stage receipt 会保存
decision receipt 路径/hash，status/replay 会重新验证这条绑定。
