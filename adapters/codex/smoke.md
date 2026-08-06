# Codex adapter smoke

## 当前可执行（Scaffold）

```powershell
python -X utf8 scripts/validate_codex_adapter.py
python -X utf8 -m pytest scripts/tests/test_hooks.py -q -o addopts=''
```

## 首个 Skill 完成后

在 Windows 中文路径的临时小说项目中执行：

```powershell
python -X utf8 scripts/webnovel.py --project-root "<项目根>" project-status --format summary
python -X utf8 scripts/webnovel.py --project-root "<项目根>" doctor --format text
```

并在 Codex Desktop 中确认：插件可发现、Skill 会触发、hook 经用户信任后运行、输出无乱码、没有修改项目事实数据。

## 完整写章验收（后续）

必须覆盖 `prewrite → context → draft → review → precommit → chapter-commit → projection → postcommit`，并人工注入一次 projection 失败验证 retry/replay。单独生成正文不算通过。
