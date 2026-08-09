from __future__ import annotations


def test_m4_smoke_fallback_kills_owned_process_when_cli_stop_is_blocked(monkeypatch, tmp_path):
    import codex_m4_smoke

    calls: list[tuple[str, ...]] = []
    killed: list[int] = []

    def _blocked_cli(_project, _env, *args):
        calls.append(tuple(args))
        return 1, {"status": "blocked"}

    monkeypatch.setattr(codex_m4_smoke, "_run_cli", _blocked_cli)
    monkeypatch.setattr(codex_m4_smoke, "_owned_process_cleanup", killed.append)

    codex_m4_smoke._cleanup_owned_dashboard(tmp_path, {}, 4321)

    assert calls == [("dashboard", "stop")]
    assert killed == [4321]


def test_m4_smoke_skips_fallback_only_after_confirmed_not_running(monkeypatch, tmp_path):
    import codex_m4_smoke

    responses = iter(
        [
            (0, {"status": "stopped"}),
            (0, {"status": "not_running"}),
        ]
    )
    killed: list[int] = []
    monkeypatch.setattr(codex_m4_smoke, "_run_cli", lambda *args: next(responses))
    monkeypatch.setattr(codex_m4_smoke, "_owned_process_cleanup", killed.append)

    codex_m4_smoke._cleanup_owned_dashboard(tmp_path, {}, 4321)

    assert killed == []
