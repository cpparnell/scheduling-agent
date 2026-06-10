import logging
import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

logger = logging.getLogger(__name__)

CHAT_DB = Path.home() / "Library" / "Messages" / "chat.db"


class _ChatDBHandler(FileSystemEventHandler):
    def __init__(self, callback, debounce_seconds: float = 5.0):
        self._callback = callback
        self._debounce = debounce_seconds
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def on_modified(self, event):
        if event.src_path == str(CHAT_DB):
            self._schedule()

    def on_created(self, event):
        if event.src_path == str(CHAT_DB):
            self._schedule()

    def _schedule(self):
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._debounce, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self):
        with self._lock:
            self._timer = None
        try:
            self._callback()
        except Exception:
            logger.exception("Error in chat.db change callback")


def watch(callback, debounce_seconds: float = 5.0) -> Observer:
    """
    Watch chat.db for changes and call `callback` after debounce_seconds
    of quiet time. Returns the running Observer (call .stop() to halt).
    """
    handler = _ChatDBHandler(callback, debounce_seconds)
    observer = Observer()
    observer.schedule(handler, str(CHAT_DB.parent), recursive=False)
    observer.start()
    logger.info("Watching %s for changes (debounce=%.1fs)", CHAT_DB, debounce_seconds)
    return observer
