# 大纲层级与增量边界

规划使用三层视图：总纲确定全书方向，卷纲确定当前卷承诺与危机链，章纲提供最近
可执行节点。`webnovel-plan` 只增量细化指定卷，不重写全书，也不清空设定集。

## 层级职责

- 总纲：核心矛盾、分卷终点、不可破坏的人设/力量/世界边界；
- 卷节拍：当前卷目标、至少三次危机、中段反转、卷末高潮与新问题；
- 卷时间线：时间体系、跨度、锚点、转场和倒计时；
- 详细章纲：每章目标、代价、节点、禁区、时间和钩子；
- 总纲写回：只包含规划中显式新增的下一卷锚点、伏笔与开放环。

已有完成卷必须承接角色状态、关系变化、能力等级和未回收伏笔。设定冲突不靠规划
文件偷偷修正；先列 blocker，让作者选择沿用总纲、修改设定或暂停。

## 规划粒度

全书保持粗纲、当前卷保持细纲、未来卷保留调整空间。章纲描述要发生的事实和因果，
不锁死具体对白与修辞。读者反馈或创作调整需要变更时，创建新 run，重新验证受影响
批次；不要在已验证 artifact 上静默改字。

## 提升规则

四份产物先进入 run staging 并绑定同一内容 hash。manifest 与每个 artifact hash 全部通过后，
无冲突可直接原子提升；有 authored conflict 则必须使用事务返回的固定 choice request 和精确
marker，在当前父 rollout 中等待用户选择，再用 `plan-transaction decision --request-file`
生成同父任务 receipt。只有 scope 与 rollout prefix 均可信绑定的 `replace` 可在锁内重验后
覆盖，`keep`/`cancel` 零事实写；裸 token 只是公开 challenge，永远不是用户授权。后续 master
outline、state、contracts 各有独立 decision/stage receipt，不能跨步骤复用，失败从该步恢复。
