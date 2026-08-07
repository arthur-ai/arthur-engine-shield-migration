"""Fixtures for the shield_migration_scripts tests.

The scripts are standalone modules that live outside src/ and import each other
as siblings, so the directory has to be on sys.path before they can be imported
— the same thing tests/__init__.py does for src/.

Nothing here touches the network, a database, or the clock: every test drives
rendering off an injected fake clock so assertions about throttling and rate are
deterministic.
"""

import io
import os
import sys

import pytest

SCRIPTS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "../../../shield_migration_scripts"),
)

if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)


class FakeClock:
    """Stand-in for time.monotonic that only moves when a test moves it."""

    def __init__(self, start=0.0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds
        return self.t


class CaptureStream(io.StringIO):
    """StringIO with a settable isatty(), so both render paths are testable."""

    def __init__(self, tty=False):
        super().__init__()
        self._tty = tty

    def isatty(self):
        return self._tty

    @property
    def frames(self):
        """The rendered frames, split on the carriage returns and newlines the
        writer uses to separate them."""
        return [f for f in self.getvalue().replace("\r", "\n").split("\n") if f.strip()]


@pytest.fixture
def fake_clock():
    return FakeClock()


@pytest.fixture
def tty_stream():
    return CaptureStream(tty=True)


@pytest.fixture
def piped_stream():
    return CaptureStream(tty=False)


@pytest.fixture
def stub_conn():
    """A SQLAlchemy-shaped connection whose counts are canned.

    `scalar_one` returns values from `results` in order, falling back to
    `default`. `executed` records (sql, params) for assertions.
    """

    class Result:
        def __init__(self, value):
            self._value = value

        def scalar_one(self):
            return self._value

        def scalar(self):
            return self._value

        def mappings(self):
            return self._value

    class Url:
        database = "arthur_shield"

    class FakeEngine:
        url = Url()

    class StubConn:
        def __init__(self):
            self.engine = FakeEngine()
            self.executed = []
            self.results = []
            self.default = 0

        def execute(self, stmt, params=None):
            self.executed.append((str(stmt), params))
            if self.results:
                return Result(self.results.pop(0))
            return Result(self.default)

    return StubConn()
