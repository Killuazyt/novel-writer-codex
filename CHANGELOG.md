# Changelog

## 0.1.0 - 2026-08-06

- 初始化 `novel-writer-codex` Git 仓库与 Codex 插件清单。
- 从 `webnovel-writer` 锁定基线原样导入可复用 runtime、资源、Dashboard 与测试资产；宿主绑定点仍在迁移。
- 增加 GPL/上游归属、仓库说明与 Codex 迁移审计。
- 初步适配 `SessionStart` 与危险直写 `PreToolUse` hooks，并覆盖非小说目录静默与常见绕过载荷。
- 修复上游测试夹具在不支持 `TemporaryDirectory(delete=...)` 的 Python 版本上的兼容性。

### M2 - 2026-08-07

- 增加不可变项目解析结果和稳定 `where --format json`，固定 Codex-native 优先级；显式书项目路径不再由 registry 修正。
- 新绑定仅写明确 workspace 的 `.codex` pointer 与 `WEBNOVEL_HOME` registry；旧 `.claude` pointer/registry 仅只读。
- 参考资料逐文件按项目 `.codex`、旧 `.claude`、插件内置顺序解析并报告来源；Story System 与小说数据合同保持不变。
- package/version/release 校验切换为 `.codex-plugin/plugin.json` 与单仓库根，并增加生产入口宿主中立静态扫描。
- 新增离线 upstream `check`/`prepare`，冻结源 330/330 文件一致，暂存目录可重复复用且不覆盖工作树。
