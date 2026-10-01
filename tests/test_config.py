import json

from scheduling_agent import config


def test_first_run_writes_defaults_and_returns_them():
    assert not config.CONFIG_FILE.exists()
    cfg = config.load()
    assert cfg == config.DEFAULTS
    # The file is materialized on disk for the user to edit.
    on_disk = json.loads(config.CONFIG_FILE.read_text())
    assert on_disk == config.DEFAULTS


def test_first_run_returns_a_copy_not_the_defaults_object():
    cfg = config.load()
    cfg["lookback_days"] = 999
    assert config.DEFAULTS["lookback_days"] == 7


def test_user_values_override_defaults():
    config.CONFIG_DIR.mkdir(exist_ok=True)
    config.CONFIG_FILE.write_text(json.dumps({"lookback_days": 30, "target_calendar": "Work"}))
    cfg = config.load()
    assert cfg["lookback_days"] == 30
    assert cfg["target_calendar"] == "Work"
    # Unspecified keys still come from defaults.
    assert cfg["confidence_threshold"] == config.DEFAULTS["confidence_threshold"]


def test_partial_config_fills_missing_defaults():
    config.CONFIG_DIR.mkdir(exist_ok=True)
    config.CONFIG_FILE.write_text(json.dumps({"blocked_contacts": ["+15551234567"]}))
    cfg = config.load()
    assert cfg["blocked_contacts"] == ["+15551234567"]
    for key in config.DEFAULTS:
        assert key in cfg


def test_unknown_keys_are_preserved():
    config.CONFIG_DIR.mkdir(exist_ok=True)
    config.CONFIG_FILE.write_text(json.dumps({"experimental_flag": True}))
    cfg = config.load()
    assert cfg["experimental_flag"] is True


def test_malformed_json_falls_back_to_defaults():
    config.CONFIG_DIR.mkdir(exist_ok=True)
    config.CONFIG_FILE.write_text("{ this is not valid json")
    cfg = config.load()
    assert cfg == config.DEFAULTS


def test_non_object_json_falls_back_to_defaults():
    config.CONFIG_DIR.mkdir(exist_ok=True)
    config.CONFIG_FILE.write_text(json.dumps(["a", "list"]))
    cfg = config.load()
    assert cfg == config.DEFAULTS


def test_default_backends_are_jev_detection_claude_dedup():
    # Chosen from the v0.13 eval comparison: Jev matches Claude on detection
    # at ~1/3 the cost, but its dedup adjudicator lost ~20 points.
    cfg = config.load()
    assert cfg["detector_backend"] == "jev"
    assert cfg["dedup_backend"] == "claude"
    assert cfg["jev_thresholds"] == {}


def _jev_cfg():
    return {**config.DEFAULTS, "detector_backend": "jev", "dedup_backend": "jev"}


def test_resolve_backends_downgrades_jev_without_key_and_warns_once(monkeypatch, caplog):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setattr(config, "_warned_no_jev_key", False)
    cfg = _jev_cfg()
    with caplog.at_level("WARNING"):
        first = config.resolve_backends(cfg)
        second = config.resolve_backends(cfg)
    assert first["detector_backend"] == second["detector_backend"] == "claude"
    assert first["dedup_backend"] == "claude"
    assert cfg["detector_backend"] == "jev"  # input not mutated
    # Config reloads every poll; the warning must not repeat every poll.
    assert sum("TYPESAFE_API_KEY is not set" in r.message for r in caplog.records) == 1


def test_resolve_backends_treats_blank_key_as_missing(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "   ")
    monkeypatch.setattr(config, "_warned_no_jev_key", False)
    assert config.resolve_backends(_jev_cfg())["detector_backend"] == "claude"


def test_resolve_backends_keeps_jev_with_key(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-test")
    cfg = _jev_cfg()
    assert config.resolve_backends(cfg) == cfg


def test_resolve_backends_is_a_noop_for_claude(monkeypatch, caplog):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setattr(config, "_warned_no_jev_key", False)
    cfg = {**config.DEFAULTS, "detector_backend": "claude", "dedup_backend": "claude"}
    with caplog.at_level("WARNING"):
        assert config.resolve_backends(cfg) == cfg
    assert not caplog.records
