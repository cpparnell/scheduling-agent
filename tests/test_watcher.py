import threading
import time
from types import SimpleNamespace

from scheduling_agent import watcher


def _evt(path):
    return SimpleNamespace(src_path=path)


def _make_handler(debounce=0.05):
    fired = threading.Event()
    calls = []

    def cb():
        calls.append(1)
        fired.set()

    handler = watcher._ChatDBHandler(cb, debounce_seconds=debounce)
    return handler, fired, calls


def test_on_modified_fires_callback_after_debounce():
    handler, fired, calls = _make_handler(0.05)
    handler.on_modified(_evt(str(watcher.CHAT_DB)))
    # Still debouncing immediately after the event.
    assert not fired.is_set()
    assert fired.wait(2.0)
    assert len(calls) == 1


def test_on_created_fires_callback():
    handler, fired, calls = _make_handler(0.05)
    handler.on_created(_evt(str(watcher.CHAT_DB)))
    assert fired.wait(2.0)
    assert len(calls) == 1


def test_rapid_events_coalesce_into_single_fire():
    handler, fired, calls = _make_handler(0.1)
    for _ in range(5):
        handler.on_modified(_evt(str(watcher.CHAT_DB)))
        time.sleep(0.01)
    assert fired.wait(2.0)
    # Give any erroneously-scheduled extra timers a chance to fire.
    time.sleep(0.15)
    assert len(calls) == 1


def test_ignores_events_for_other_paths():
    handler, fired, calls = _make_handler(0.05)
    handler.on_modified(_evt("/some/other/file"))
    handler.on_created(_evt("/another/path"))
    assert not fired.wait(0.3)
    assert calls == []


def test_callback_exception_does_not_propagate():
    def boom():
        raise RuntimeError("kaboom")

    handler = watcher._ChatDBHandler(boom, debounce_seconds=0.01)
    # A raising callback must not escape _fire (which would kill the timer
    # thread and stop the observer from reacting to future changes).
    handler._fire()
    assert handler._timer is None
