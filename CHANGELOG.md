# Changelog

## 0.1.0 - 2026-08-06

- 初始化 `novel-writer-codex` Git 仓库与 Codex 插件清单。
- 从 `webnovel-writer` 锁定基线原样导入可复用 runtime、资源、Dashboard 与测试资产；宿主绑定点仍在迁移。
- 增加 GPL/上游归属、仓库说明与 Codex 迁移审计。
- 初步适配 `SessionStart` 与危险直写 `PreToolUse` hooks，并覆盖非小说目录静默与常见绕过载荷。
- 修复上游测试夹具在不支持 `TemporaryDirectory(delete=...)` 的 Python 版本上的兼容性。
