# Novel Writer Codex

`novel-writer-codex` 是 [lingfengQAQ/webnovel-writer](https://github.com/lingfengQAQ/webnovel-writer) 的 Codex 适配项目。目标不是重写创作引擎，而是保留原项目成熟的 Python runtime、Story System、记忆、审查与投影链，只为 Codex 增加一层薄适配。

> 当前阶段：`0.1.0` 基础迁移中，尚未发布可供日常创作使用的完整版本。
>
> 本仓库是基于上游 `master@2041abad78211e29a67a2f0c64b2a97a747dce57` 的第三方 GPL-3.0 派生修改，不是上游官方发行版。

## 当前进度

| 能力 | 状态 | 说明 |
|---|---|---|
| Git / Codex 插件外壳 | 已完成 | 主分支为 `main`，已有 `.codex-plugin/plugin.json` |
| M0 上游冻结与仓库卫生 | 已完成 | 锁定 330 个上游文件、逐文件哈希和只读 remote；保护边界与文本规则已验证 |
| M1 测试隔离与宿主契约 | 已完成 | 798 项可安全收集；当前 Codex 有效全集 732 项通过，coverage 90.16% |
| Python runtime 与资源 | 基线已导入 | `scripts/`、`references/`、`templates/`、`dashboard/`、`evals/` 来自锁定上游，宿主绑定点仍在逐项清理 |
| Codex hooks | 协议初适配 | 已覆盖非小说项目静默和常见绕过载荷，仍需真实安装/信任 smoke；hook 不是完整安全边界 |
| 8 个业务 Skills | 未开放 | Claude Code 专用表达尚未逐项改写，避免把未适配能力伪装成可用 |
| 4 个专用 Agents | 设计已映射 | Codex 插件分发与项目级自定义 Agent 需要分层处理 |
| 完整写章链 | 未验收 | `prewrite → review → commit → projection → postcommit` 必须端到端验证后再标记可用 |

## 架构原则

```text
Codex Skill / subagent / hook
              ↓
        scripts/webnovel.py
              ↓
          data_modules
              ↓
     .story-system commits
              ↓
      .webnovel read models
```

- `scripts/webnovel.py` 及其数据模块是唯一业务真源。
- 宿主适配层只负责发现、工具映射和 Agent 调度。
- 章节事实只能经 runtime 提交；不得直接写 commit 或投影数据库。
- 缺失或版本不匹配的专用 Agent 必须阻断相关能力并引导执行 `$webnovel-setup`；不得由主 Agent 冒充。

## 本地验证

```powershell
python -X utf8 scripts/validate_codex_adapter.py --format json
python -X utf8 -m pytest scripts/tests/test_hooks.py -q -o addopts='' -p scripts.pytest_bootstrap -p pytest_asyncio.plugin -p pytest_timeout -p no:cacheprovider --strict-markers --timeout=30 --timeout-method=thread
```

M1 隔离已验收。所有 pytest 运行必须通过隔离 runner，或像上面的定向命令一样显式加载 bootstrap；不要直接执行原始 `pytest`。`full` 表示当前 Codex 有效全集，不会执行冻结的 Claude 契约：

```powershell
powershell -NoProfile -File scripts/run_tests.ps1 -Mode smoke
powershell -NoProfile -File scripts/run_tests.ps1 -Mode collect
powershell -NoProfile -File scripts/run_tests.ps1 -Mode full
powershell -NoProfile -File scripts/run_tests.ps1 -Mode upstream-collect
```

唯一详细实施清单见 [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md)；迁移差异与能力边界见 [docs/PORTING.md](docs/PORTING.md)；上游归属与基线见 [UPSTREAM.md](UPSTREAM.md)。

## 许可证

本项目是 GPL-3.0 派生移植，继续按 [GNU GPL v3](LICENSE) 发布。原项目版权与归属不因本移植而改变；下游修改范围及冻结基线记录在 [UPSTREAM.md](UPSTREAM.md) 和 `upstream-lock.json`。
