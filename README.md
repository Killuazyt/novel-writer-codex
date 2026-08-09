# Novel Writer Codex

<!-- novel-writer-codex-version: 0.3.0 -->

`novel-writer-codex` 是 [lingfengQAQ/webnovel-writer](https://github.com/lingfengQAQ/webnovel-writer) 的 Codex 适配项目。目标不是重写创作引擎，而是保留原项目成熟的 Python runtime、Story System、记忆、审查与投影链，只为 Codex 增加一层薄适配。

> 当前代码版本为 `0.3.0` 本机 Full-write Beta，尚未创建 tag 或 GitHub Release。9 个 Skill 源适配、M5/M6 自动核心与三类可信父任务裁决均已落地；当前工作区的 5 个项目 Agent 已安装并回读为 current，个人 marketplace 已建立。插件点击安装、新任务发现和真实完整写章链仍待关闭；CI、发布 Marketplace、多平台和外部用户分发暂缓。
>
> 本仓库是基于上游 `master@2041abad78211e29a67a2f0c64b2a97a747dce57` 的第三方 GPL-3.0 派生修改，不是上游官方发行版。

## 当前进度

| 能力 | 状态 | 说明 |
|---|---|---|
| Git / Codex 插件外壳 | 已完成 | 主分支为 `main`，已有 `.codex-plugin/plugin.json` |
| M0 上游冻结与仓库卫生 | 已完成 | 锁定 330 个上游文件、逐文件哈希和只读 remote；保护边界与文本规则已验证 |
| M1 测试隔离与宿主契约 | 已完成 | 798 项可安全收集；当前 Codex 有效全集 732 项通过，coverage 90.16% |
| M2 宿主中立 runtime 与上游同步 | 已完成 | 862 项安全收集；789 项当前 Codex 契约通过，coverage 90.19%；冻结源 330/330 匹配 |
| M3 Setup、交互与 Agent 公共框架 | 已完成 | 1055 项安全收集；978 passed、2 skipped、75 deselected，coverage 90.50%；真实双父模型/8 子任务路由通过；Hook 现场 smoke 作为可选安全增强保留 |
| M4 Read-only Alpha | 实现完成，未发布 | 1132 项安全收集；1042 passed、3 skipped、87 deselected，coverage 90.34%；Windows 中文路径 Doctor/Query/Dashboard 安全 smoke 通过；真实安装后的新任务发现证据待另行采集 |
| M5 Controlled-write Beta | 自动实现完成，live gate 待验收 | Learn、单章 Review、Init、Plan 绿地与 authored-conflict 可信父任务 receipt 已接线；真实 Apply/Agent/父任务选择仍待现场验证 |
| M6 Full-write Beta | 自动核心完成，live gate 待验收 | 范围 Review、default/fast/minimal 写章、blocking 逐 issue resolution 和作者正文冲突恢复 receipt 已实现；真实四 Agent/投影失败链仍待端到端验证 |
| Python runtime 与资源 | 宿主中立基础已完成 | 项目定位、`.codex` pointer、参考资料来源和旧 `.claude` 只读 fallback 已统一；Story System 与小说数据合同未改 |
| Codex hooks | 自动安全边界已验收 | 协议、绕过负例和证据校验器已完成；`/hooks` 未信任→持久化信任现场 smoke 为可选增强；hook 不是完整安全边界 |
| `$webnovel-setup` | 已实现并在当前工作区 Apply | 零写入 check、有限选项确认、托管 apply、冲突/回滚/幂等与新任务提示均已验证；5 个 Agent 当前回读为 current |
| Read-only Skills | 已实现，待安装发现 | `$webnovel-doctor`、`$webnovel-query`、`$webnovel-dashboard` 已通过本地合同与真实 loopback smoke；未用静态校验冒充新任务发现 |
| 9 个 Skills | 源适配与自动合同完成 | Setup/Doctor/Query/Dashboard 已完成既有本地 gate；Learn/Review/Init/Plan/Write 的自动路径和三类可信父任务裁决已接线，真实安装/模型/用户选择 live gate 尚未关闭 |
| 5 个目标 Agents | 已实现、安装并实机验证 | 五份合同与项目 TOML 已由 Setup 安装；Sol/Terra 两种父模型下，context/writer/reviewer/data 的实际 rollout 均为 `gpt-5.6-luna / medium`；deconstruction 继承父配置 |
| 完整写章链 | 自动核心已验，live 未验收 | 精确 Agent lineage、blocking/正文冲突 receipt、commit/projection/postcommit/current-truth 与恢复测试已通过；真实四 Luna Agent 和 projection 故障恢复仍必须端到端验证 |

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
- 主对话负责规划、用户选择与编排；正文起草/润色和章节审查由固定 `gpt-5.6-luna` 的项目 Agent 执行，模型不可用时不得静默回退。

## 本地验证

```powershell
python -X utf8 scripts/validate_codex_adapter.py --format json
python -X utf8 scripts/validate_plugin_package.py --strict --format json
python -X utf8 scripts/validate_repository_hygiene.py --format json
python -X utf8 scripts/sync_plugin_version.py --check --expected-version 0.3.0
python -X utf8 scripts/webnovel.py codex-setup --workspace-root "<工作区>" --check --format json
$UpstreamRoot = "PATH_TO_LOCAL_UPSTREAM_CHECKOUT"
python -X utf8 scripts/upstream_sync.py check --source-root $UpstreamRoot --sha 2041abad78211e29a67a2f0c64b2a97a747dce57 --format json
python -X utf8 -m pytest scripts/tests/test_hooks.py -q -o addopts='' -p scripts.pytest_bootstrap -p pytest_asyncio.plugin -p pytest_timeout -p no:cacheprovider --strict-markers --timeout=30 --timeout-method=thread
```

M1 隔离、M2 宿主中立、M3 与 M4 自动 gate 已验收；M5/M6 最新安全收集 1893 项，`full` 为 1771 passed、15 skipped、107 deselected，`scripts/data_modules` coverage 90.41%，9 个真实宿主保护路径零变化。用户侧 Hook 持久化信任现场 smoke 仍只是可选安全增强。三类父任务裁决自动实现已完成，但真实安装/新顶层任务、Apply/Agent/现场回答和完整写章证据没有用静态测试冒充，详见实施计划的 pending 清单。所有 pytest 运行必须通过隔离 runner，或像上面的定向命令一样显式加载 bootstrap；不要直接执行原始 `pytest`。`full` 表示当前 Codex 有效全集，不会执行冻结的 Claude 契约：

```powershell
powershell -NoProfile -File scripts/run_tests.ps1 -Mode smoke
powershell -NoProfile -File scripts/run_tests.ps1 -Mode collect
powershell -NoProfile -File scripts/run_tests.ps1 -Mode full
powershell -NoProfile -File scripts/run_tests.ps1 -Mode upstream-collect
```

唯一详细实施清单见 [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md)；迁移差异与能力边界见 [docs/PORTING.md](docs/PORTING.md)；上游归属与基线见 [UPSTREAM.md](UPSTREAM.md)。

## 许可证

本项目是 GPL-3.0 派生移植，继续按 [GNU GPL v3](LICENSE) 发布。原项目版权与归属不因本移植而改变；下游修改范围及冻结基线记录在 [UPSTREAM.md](UPSTREAM.md) 和 `upstream-lock.json`。
