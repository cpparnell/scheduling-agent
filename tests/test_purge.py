from scheduling_agent import main, state


def test_purge_removes_state_file_and_logs_dir(monkeypatch, tmp_path, capsys):
    logs_dir = tmp_path / "logs" / "stdout"
    logs_dir.mkdir(parents=True)
    (logs_dir / "2026-01-01_00-00-00.log").write_text("hello")
    monkeypatch.setattr(main, "LOGS_DIR", logs_dir)
    monkeypatch.setattr(main, "LAUNCHD_LOG_DIR", tmp_path / "no-launchd-logs")

    state.update_timestamp(123)  # materializes state.STATE_FILE
    assert state.STATE_FILE.exists()

    main.purge()

    assert not state.STATE_FILE.exists()
    assert not logs_dir.parent.exists()
    out = capsys.readouterr().out
    assert "Removed:" in out


def test_purge_removes_launchd_log_dir_when_present(monkeypatch, tmp_path, capsys):
    logs_dir = tmp_path / "logs" / "stdout"  # never created
    monkeypatch.setattr(main, "LOGS_DIR", logs_dir)
    launchd_log_dir = tmp_path / "Library" / "Logs" / "scheduling-agent"
    launchd_log_dir.mkdir(parents=True)
    (launchd_log_dir / "launchd.log").write_text("supervisor output")
    monkeypatch.setattr(main, "LAUNCHD_LOG_DIR", launchd_log_dir)

    main.purge()

    assert not launchd_log_dir.exists()
    out = capsys.readouterr().out
    assert str(launchd_log_dir) in out


def test_purge_is_noop_when_nothing_to_remove(monkeypatch, tmp_path, capsys):
    logs_dir = tmp_path / "logs" / "stdout"  # never created
    monkeypatch.setattr(main, "LOGS_DIR", logs_dir)
    monkeypatch.setattr(main, "LAUNCHD_LOG_DIR", tmp_path / "no-launchd-logs")
    assert not state.STATE_FILE.exists()

    main.purge()

    out = capsys.readouterr().out
    assert "Nothing to remove." in out


def test_purge_flag_calls_purge_and_skips_watcher(monkeypatch):
    called = {"purge": False, "watch": False}
    monkeypatch.setattr(main, "purge", lambda: called.__setitem__("purge", True))
    monkeypatch.setattr(main.watcher, "watch", lambda *a, **k: called.__setitem__("watch", True))
    monkeypatch.setattr("sys.argv", ["scheduling-agent", "--purge"])

    main.main()

    assert called["purge"] is True
    assert called["watch"] is False
