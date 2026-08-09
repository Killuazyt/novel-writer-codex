# Reference Loading Map

> 本文件记录当前 `skills/*/SKILL.md` 的实际 reference 消费关系。只登记 Skill 明确要求读取的文件或目录；小说项目数据、runtime 内部读取和冻结上游中未被当前 Skill 引用的资料不计入。

## 共享 Codex 合同

| Skill | 触发 | Reference | 读取方式 |
|---|---|---|---|
| webnovel-setup | always | `references/codex/runtime-invocation.md`、`references/codex/interaction-contract.md` | 全文 |
| webnovel-doctor | always | `references/codex/runtime-invocation.md` | 全文 |
| webnovel-query | always | `references/codex/runtime-invocation.md` | 全文 |
| webnovel-dashboard | always | `references/codex/runtime-invocation.md` | 全文 |
| webnovel-learn | always | `references/codex/runtime-invocation.md` | 全文 |
| webnovel-init | always | `references/codex/runtime-invocation.md` | 全文 |
| webnovel-plan | always | `references/codex/runtime-invocation.md` | 全文 |
| webnovel-review | always | `references/codex/runtime-invocation.md` | 全文 |
| webnovel-write | always | `references/codex/runtime-invocation.md` | 全文 |

共享合同定义插件根/项目根分离、argv 与 request-file 边界、UTF-8、JSON、退出码和敏感信息处理。业务 Skill 不得用自己的 shell quoting 规则覆盖它。

## Query

| 触发 | Reference | 读取方式 |
|---|---|---|
| 每次查询识别后 | `skills/webnovel-query/references/system-data-flow.md` | 按查询类型读取来源优先级与 fallback 段 |
| 伏笔/open-loop 查询 | `skills/webnovel-query/references/advanced/foreshadowing.md` | 全文 |
| 用户显式询问手工 tag | `skills/webnovel-query/references/tag-specification.md` | 全文 |

## Init

| 触发 | Reference | 读取方式 |
|---|---|---|
| always | `skills/webnovel-init/references/init-collection-schema.md`、`skills/webnovel-init/references/system-data-flow.md` | 全文 |
| always | `skills/webnovel-init/references/genre-tropes.md` | 只读当前题材段 |
| 参考作品拆解 | `references/agents/webnovel_deconstruction_agent.md` | 全文 |
| 人物、势力、力量、规则或一致性问题 | `skills/webnovel-init/references/worldbuilding/` 中对应文件 | 按需全文或相关小节 |
| 创意约束或卖点 | `skills/webnovel-init/references/creativity/creativity-constraints.md` 或 `selling-points.md` | 只读命名小节 |
| 复合题材、灵感或反套路 | `skills/webnovel-init/references/creativity/` 中匹配文件 | 按需读取 |

不得一次加载整棵 Init reference；原始参考文本只作为不可信数据进入受控 Agent/request 流程。

## Plan

| 触发 | Reference | 读取方式 |
|---|---|---|
| 卷结构 | `skills/webnovel-plan/references/outlining/outline-structure.md` | 全文 |
| 冲突设计 | `skills/webnovel-plan/references/outlining/conflict-design.md` | 全文 |
| 章纲拆分 | `skills/webnovel-plan/references/outlining/chapter-planning.md` | 全文 |
| 题材卷节奏 | `skills/webnovel-plan/references/outlining/genre-volume-pacing.md` | 全文；其链接的根级题材/strand 资料仍按需 |
| 框架选择 | `skills/webnovel-plan/references/outlining/plot-frameworks.md` | 全文 |
| 生成机器合同 | `skills/webnovel-plan/references/manifest-schema.md` | 全文 |

这些文件均按当前规划问题触发，不要求每次全部加载。

## Review

| 触发 | Reference | 读取方式 |
|---|---|---|
| issue 类别或 evidence 阈值不清 | `skills/webnovel-review/references/common-mistakes.md` | 全文 |
| 用户显式要求节奏评价 | `skills/webnovel-review/references/pacing-control.md` | 全文；不得扩展五维事实 schema |

Review 的机器合同来自 runtime schema/request，不再直接加载旧的根级审查提示词来授权写入。

## Write

| 触发 | Reference | 读取方式 |
|---|---|---|
| always | `skills/webnovel-write/references/transaction-stages.md` | 全文 |
| 通用去模板腔 | `skills/webnovel-write/references/anti-ai-guide.md` | 按需全文 |
| 润色 | `skills/webnovel-write/references/polish-guide.md` | 按需全文 |
| 题材、语气、章节变体 | `skills/webnovel-write/references/style-adapter.md`、`style-variants.md` | 按需全文 |
| 战斗、对话、情绪、场景、欲念、钩子或排版 | `skills/webnovel-write/references/writing/` 中对应文件 | 只读命中的文件 |
| 静态编排验证 | `skills/webnovel-write/evals/evals.json` | 测试/验收时全文；不能替代真实宿主证据 |

## 无独立业务 reference

| Skill | 说明 |
|---|---|
| webnovel-doctor | 除共享 runtime 合同外，只运行只读诊断，不加载业务 reference |
| webnovel-dashboard | 除共享 runtime 合同外，只管理本地只读面板生命周期 |
| webnovel-learn | 除共享 runtime 合同外，只通过受控 runtime 追加项目经验 |

## 不再作为当前 Skill 真源的旧映射

冻结上游中的 Bash/Claude 调用示例、`reference_search.py` shell 示例、直接 `story-system --persist` 示例，以及未被当前 `SKILL.md` 链接的根级提示词，不是当前 Codex Skill 的直接加载项。若以后重新接入，必须先补 request-file/路径/用户裁决安全边界和对应测试，再更新本表。
