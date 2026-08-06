# Upstream attribution

本仓库是第三方 Codex 适配项目，不是上游官方发行版。

> 这是对上游源码的派生修改发行；文件内容、宿主接口、测试与发布结构均可能与上游不同。使用和再分发继续受 GPL-3.0 约束。

- 上游项目：[lingfengQAQ/webnovel-writer](https://github.com/lingfengQAQ/webnovel-writer)
- 上游作者/维护者：`lingfengQAQ` 及原项目贡献者
- 导入基线：`master@2041abad78211e29a67a2f0c64b2a97a747dce57`
- 上游 manifest 版本：`6.2.1`
- 首次适配日期：2026-08-06
- 目标仓库：[Killuazyt/novel-writer-codex](https://github.com/Killuazyt/novel-writer-codex)
- 许可证：GNU General Public License v3.0

## 本项目的主要修改

- 将 Claude Code 插件清单替换为 Codex 插件清单。
- 将宿主专用 Skill、Agent、工具名和环境变量逐步改写为 Codex 表达。
- 为 Codex hooks 增加跨平台命令、官方拒绝协议和 `apply_patch` 检查。
- 增加 Codex 能力记录、工具/Agent 映射、smoke 与迁移文档。
- 保留并复用原项目 Python runtime、Story System、模板、参考资料、Dashboard 与测试资产。

后续同步上游时，必须更新导入基线、变更日期与差异说明，并重新运行迁移审计和测试。
