# 章节润色顺序

润色是“修问题并改善表达”，不是重写剧情。输入必须绑定当前 run 的 writer artifact
和同 run reviewer 结果；不能拿旧审查报告处理新正文。

## 固定顺序

1. 先处理 reviewer 的 critical/high 结构化问题；无法定点修复的 blocking issue 等待
   作者选择，不能继续提交。
2. 再检查人物口吻、节奏、信息密度、章节类型和期待锚点。
3. 按 [style-adapter.md](style-adapter.md) 做题材与语气适配。
4. 按 [writing/typesetting.md](writing/typesetting.md) 做移动端排版。
5. 按 [anti-ai-guide.md](anti-ai-guide.md) 逐段终检。
6. 检查降智推进、强行误会、无代价宽恕、工具人配角和无解释双标；命中时补足
   动机、阻力、代价中的至少两项，但不得改变既定事件结果。

修复 setting、timeline、continuity 时只能恢复合法事实或补充必要过渡；不能凭润色
新增能力、关系、地点或伏笔。writer final 一次调用同时完成定点修复和润色，输出新的
`polished.md`，禁止以同名文件为输入再次覆盖。

## 输出合同

writer manifest 应记录：修复问题 ID、未处理问题与原因、anti-AI 结果、毒点检查、
偏离项、最终正文路径/hash/字数。default/fast 的 `anti_ai_force_check` 为 fail、critical
未清零或存在未裁决 blocking 时不得提升。minimal 仅做事实不变的排版，并在 receipt
中明确深检 skipped。
