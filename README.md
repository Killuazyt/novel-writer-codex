# Novel Writer Codex

<!-- novel-writer-codex-version: 0.3.0 -->

`novel-writer-codex` 是 [lingfengQAQ/webnovel-writer](https://github.com/lingfengQAQ/webnovel-writer) 的 Codex 适配项目。目标不是重写创作引擎，而是保留原项目成熟的 Python runtime、Story System、记忆、审查与投影链，只为 Codex 增加一层薄适配。

> 当前代码版本为 `0.3.0` 本机 Full-write Beta，尚未创建 tag 或 GitHub Release。9 个 Skill、5 个项目 Agent、M5/M6 自动核心与三类可信父任务裁决均已落地；第 1 章 default 写章、投影故障恢复、Doctor、独立 Review、Query、Learn 与 Dashboard 已完成真实本机验收。插件已通过本机缓存更新和独立新任务发现；另一独立父任务已完成真实 Init `Apply`、第 1 卷 Plan、prewrite 与 Doctor。CI、发布 Marketplace、多平台和外部用户分发暂缓。
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
| M4 Read-only Alpha | 实现与本机发现已验收，未发布 | Windows 中文路径 Doctor/Query/Dashboard 安全 smoke 通过；独立新顶层任务已从安装缓存发现全部只读 Skill |
| M5 Controlled-write Beta | 自动实现完成，主要本机路径已验收 | 独立真实父任务完成 Init `Apply` → 10 章 Plan → prewrite → Doctor；Query、Learn 与独立单章 full Review 也已验证。参考作品 Adopt、authored-conflict 与 blocking 有限选择的更多现场分支仍保留为后续覆盖 |
| M6 Full-write Beta | default 本机链已验收 | 第 1 章 default 模式的 Luna/medium 历史基线、accepted commit、投影失败后仅 retry、postcommit 与非 Git backup skip 已通过；当前固定子 Agent 已统一升级为 Luna/high |
| Python runtime 与资源 | 宿主中立基础已完成 | 项目定位、`.codex` pointer、参考资料来源和旧 `.claude` 只读 fallback 已统一；Story System 与小说数据合同未改 |
| Codex hooks | 自动安全边界已验收 | 协议、绕过负例和证据校验器已完成；`/hooks` 未信任→持久化信任现场 smoke 为可选增强；hook 不是完整安全边界 |
| `$webnovel-setup` | 已实现并在当前工作区 Apply | 零写入 check、有限选项确认、托管 apply、冲突/回滚/幂等与新任务提示均已验证；5 个 Agent 当前回读为 current |
| Read-only Skills | 已实现并完成本机流程 | `$webnovel-doctor`、`$webnovel-query`、`$webnovel-dashboard` 已在中文项目实跑；Dashboard 仅绑定 loopback，双健康接口 200 后已停止 |
| 9 个 Skills | 已安装并通过新任务发现 | 独立新顶层任务从安装缓存发现 9/9；缓存 manifest、Plugin Creator validator 与零写入指纹均通过 |
| 5 个目标 Agents | 已实现、安装并实机验证 | context/writer/reviewer/data 当前固定为 `gpt-5.6-luna / high`；deconstruction 继承父配置。更新后需重新执行 `$webnovel-setup` 并打开新任务 |
| 完整写章链 | default live 已验收 | `write-ch0001-737f9df2a045` 是升级前 Luna/medium 的真实历史证据；15 阶段与 projection retry 均通过，新的 Luna/high 合同由定向路由测试覆盖 |

## 如何使用

完整的安装、本地嵌入模型、新书初始化、Setup、规划、写章和常用提示词见
[《使用说明》](使用说明.md)。最短流程是：

1. 安装 Python 依赖并将 `Qwen/Qwen3-Embedding-0.6B` 下载到默认本地目录。
2. 在 Codex 中安装本插件并打开一个新任务。
3. 使用 `$webnovel-init` 初始化新书，确认预览后回复 `Apply`。
4. 在小说工作区使用 `$webnovel-setup` 安装或更新 5 个项目 Agent；Apply 后再次打开新任务。
5. 依次使用 `$webnovel-plan`、`$webnovel-doctor` 和 `$webnovel-write default`。

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
- 主对话负责规划、用户选择与编排；正文起草/润色和章节审查由固定 `gpt-5.6-luna / high` 的项目 Agent 执行，模型不可用时不得静默回退。

## 本地嵌入模型（默认，不需要 API Key）

Novel Writer Codex 默认使用本机 `Qwen/Qwen3-Embedding-0.6B`，不再要求
`EMBED_API_KEY`。该模型支持中英文等多语言、最长 32K 上下文和 1024 维向量；模型文件约
1.2 GB。模型只需显式下载一次，写章、投影和查询时始终以 `local_files_only` 加载，
runtime 不会偷偷联网下载。模型与版本要求以
[Qwen 官方模型卡](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B)为准；本地推理使用
[Sentence Transformers](https://sbert.net/docs/installation.html)。

### 1. 安装本地推理依赖

Python 需为 3.10 或更高版本：

```powershell
python -m pip install -r requirements.txt
```

若只补本地 RAG 依赖，也可以运行：

```powershell
python -m pip install -U "sentence-transformers>=2.7.0" "transformers>=4.51.0" "huggingface-hub>=0.28.0"
```

### 2. 显式下载模型

Windows PowerShell 默认目录：

```powershell
$ModelDir = Join-Path $env:USERPROFILE ".codex\novel-writer-codex\models\Qwen3-Embedding-0.6B"
hf download Qwen/Qwen3-Embedding-0.6B --local-dir $ModelDir
```

如 Hugging Face 在当前网络不可用，可安装并使用 ModelScope 官方客户端：

```powershell
python -m pip install -U modelscope
$ModelDir = Join-Path $env:USERPROFILE ".codex\novel-writer-codex\models\Qwen3-Embedding-0.6B"
modelscope download --model Qwen/Qwen3-Embedding-0.6B --local_dir $ModelDir
```

Linux/macOS 默认目录：

```bash
MODEL_DIR="${CODEX_HOME:-$HOME/.codex}/novel-writer-codex/models/Qwen3-Embedding-0.6B"
hf download Qwen/Qwen3-Embedding-0.6B --local-dir "$MODEL_DIR"
```

`hf download --local-dir` 的行为见
[Hugging Face 官方下载文档](https://huggingface.co/docs/huggingface_hub/guides/download)。

### 3. 配置项目

新项目生成的 `.env.example` 已使用下列本地默认值。复制为 `.env` 后通常无需填写路径；
只有模型下载到其他位置时才需要设置绝对 `EMBED_MODEL_PATH`：

```dotenv
EMBED_API_TYPE=local
EMBED_MODEL=Qwen/Qwen3-Embedding-0.6B
EMBED_MODEL_PATH=
EMBED_DEVICE=auto
EMBED_BATCH_SIZE=8
EMBED_NORMALIZE=true
RERANK_API_TYPE=disabled
```

`RERANK_API_TYPE=disabled` 表示完全不调用云端 rerank；混合检索会使用本地向量、BM25 与
RRF 排序。若机器内存较小，可把 `EMBED_BATCH_SIZE` 降为 `2` 或 `4`；安装了匹配的
CUDA PyTorch 时可设 `EMBED_DEVICE=cuda`，否则保持 `auto` 或显式设为 `cpu`。

下载后先运行只读 `$webnovel-doctor`；也可以直接执行它所调用的 runtime 命令：

```powershell
python -X utf8 scripts/webnovel.py --project-root "<小说项目绝对路径>" doctor --format json
```

结果中的 `rag.embed.backend`、`rag.embed.local_dependency` 与
`rag.embed.local_model` 应全部为 `ok`。从云端模型切换到本地模型后，已有章节应通过
`projections replay` 重新生成一致维度的向量；某章仅因 vector 失败时使用
`projections retry --chapter <N>`，不要重复 `chapter-commit`。

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

M1 隔离、M2 宿主中立、M3 与 M4 自动 gate 已验收；M5/M6 最新安全收集 1976 项，`full` 为 1854 passed、15 skipped、107 deselected，`scripts/data_modules` coverage 90.23%，9 个真实宿主保护路径零变化。用户侧 Hook 持久化信任现场 smoke 仍只是可选安全增强。真实安装、新顶层任务、Init `Apply`、父模型 Plan、Luna Agent 写章及投影恢复证据均来自宿主现场；fast/minimal、参考作品 Adopt、authored-conflict、blocking 与范围审查选择等扩展分支没有用静态测试冒充，详见实施计划的 pending 清单。所有 pytest 运行必须通过隔离 runner，或像上面的定向命令一样显式加载 bootstrap；不要直接执行原始 `pytest`。`full` 表示当前 Codex 有效全集，不会执行冻结的 Claude 契约：

```powershell
powershell -NoProfile -File scripts/run_tests.ps1 -Mode smoke
powershell -NoProfile -File scripts/run_tests.ps1 -Mode collect
powershell -NoProfile -File scripts/run_tests.ps1 -Mode full
powershell -NoProfile -File scripts/run_tests.ps1 -Mode upstream-collect
```

完整使用流程见 [《使用说明》](使用说明.md)；唯一详细实施清单见 [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md)；迁移差异与能力边界见 [docs/PORTING.md](docs/PORTING.md)；上游归属与基线见 [UPSTREAM.md](UPSTREAM.md)。

## 许可证

本项目是 GPL-3.0 派生移植，继续按 [GNU GPL v3](LICENSE) 发布。原项目版权与归属不因本移植而改变；下游修改范围及冻结基线记录在 [UPSTREAM.md](UPSTREAM.md) 和 `upstream-lock.json`。
