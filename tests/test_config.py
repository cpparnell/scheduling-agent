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
