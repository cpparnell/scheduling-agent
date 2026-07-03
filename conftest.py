"""Mirrors the pytest console output to logs/tests/, alongside the same
pattern used for eval runs (logs/evals/) and the live app (logs/stdout/).
"""

import re
import sys
from datetime import datetime
from pathlib import Path

LOGS_DIR = Path(__file__).parent / "logs" / "tests"

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


class _Tee:
    """Mirrors writes to stdout into a log file for the duration of the run."""

    def __init__(self, log_file):
        self._log_file = log_file
        self._real_stdout = None

    def start(self) -> None:
        self._real_stdout = sys.stdout
        sys.stdout = self

    def stop(self) -> None:
        sys.stdout = self._real_stdout

    def write(self, data: str) -> None:
        self._real_stdout.write(data)
        self._log_file.write(_ANSI_ESCAPE.sub("", data))

    def flush(self) -> None:
        self._real_stdout.flush()
        self._log_file.flush()

    def isatty(self) -> bool:
        return self._real_stdout.isatty()


def pytest_configure(config: "pytest.Config") -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOGS_DIR / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
    log_file = log_path.open("w")
    tee = _Tee(log_file)
    tee.start()
    config._test_log_file = log_file
    config._test_tee = tee

    # Force pytest's terminal writer to pick up the teed stdout rather than
    # the reference it captured before this hook ran.
    terminal_reporter = config.pluginmanager.get_plugin("terminalreporter")
    if terminal_reporter is not None:
        terminal_reporter._tw = terminal_reporter._tw.__class__(file=sys.stdout)


def pytest_unconfigure(config: "pytest.Config") -> None:
    tee = getattr(config, "_test_tee", None)
    log_file = getattr(config, "_test_log_file", None)
    if tee is not None:
        tee.stop()
    if log_file is not None:
        log_file.close()
