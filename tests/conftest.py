import json
from types import SimpleNamespace

import pytest

from scheduling_agent import config, detector, reader, state
from tests.fixtures import chatdb


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch, tmp_path):
    """Redirect all on-disk state/config to a temp dir so no test ever touches
    ~/.scheduling-agent."""
    home = tmp_path / ".scheduling-agent"
    monkeypatch.setattr(state, "STATE_DIR", home)
    monkeypatch.setattr(state, "STATE_FILE", home / "state.json")
    monkeypatch.setattr(config, "CONFIG_DIR", home)
    monkeypatch.setattr(config, "CONFIG_FILE", home / "config.json")
    return home


@pytest.fixture
def fake_chat_db(monkeypatch, tmp_path):
    """Return a builder that writes a fixture chat.db and points reader.CHAT_DB
    at it. Usage: ``fake_chat_db([{...chat...}])``."""

    def make(chats):
        path = tmp_path / "chat.db"
        chatdb.build_chat_db(path, chats)
        monkeypatch.setattr(reader, "CHAT_DB", path)
        return path

    return make


# --- Fake Anthropic client -------------------------------------------------


class _TextBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _FakeMessages:
    def __init__(self, payloads):
        self._payloads = list(payloads)
        self._i = 0
        self.calls = []  # records kwargs of each create() call

    def create(self, **kwargs):
        self.calls.append(kwargs)
        payload = self._payloads[min(self._i, len(self._payloads) - 1)]
        self._i += 1
        if isinstance(payload, Exception):
            raise payload
        text = payload if isinstance(payload, str) else json.dumps(payload)
        return SimpleNamespace(content=[_TextBlock(text)])


class FakeClient:
    """Stand-in for anthropic.Anthropic. ``payloads`` is a list consumed one per
    create() call; each item is a dict (serialized to JSON), a raw string
    (e.g. malformed JSON), or an Exception instance (raised)."""

    def __init__(self, payloads):
        self.messages = _FakeMessages(payloads)


@pytest.fixture
def fake_anthropic(monkeypatch):
    def install(payloads):
        client = FakeClient(payloads)
        monkeypatch.setattr(detector, "_client", client)
        return client

    return install
