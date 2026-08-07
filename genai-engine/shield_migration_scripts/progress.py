# progress.py
"""Live progress feedback shared by the shield migration scripts.

Stdlib only — these scripts install with

    pip install sqlalchemy psycopg2-binary requests python-dotenv

and nothing else, so no progress library is available here.

Everything is written to **stdout**. One stream keeps progress ordered with each
script's own prints, means `| tee` captures exactly what the terminal shows, and
avoids log collectors tagging healthy progress as ERROR the way they would on
stderr.

On a terminal a single line is rewritten in place. When the output is piped the
same line is appended at most once every MIGRATION_PROGRESS_INTERVAL seconds,
which holds a multi-hour run's logfile to a few hundred lines instead of the
200,000 a per-page print reaches at ~1B inferences.
"""

import os
import shutil
import sys
import threading
import time

# Seconds between renders of the live line. 0 disables it; steps still report
# their completion and duration.
PROGRESS_INTERVAL = float(os.getenv("MIGRATION_PROGRESS_INTERVAL", default=2))

# A once-a-second tick reads well in front of a human, but would add ~1,200
# lines to a logfile for a single 20-minute query, so piped output ticks slowly.
TTY_HEARTBEAT_INTERVAL = 1.0
MIN_PIPED_HEARTBEAT_INTERVAL = 30.0


def format_duration(seconds: float) -> str:
    """Human-readable duration, e.g. '1h 02m 03s', '4m 09s', '12.3s'."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    return f"{minutes}m {secs:02d}s"


# ── Line writer ───────────────────────────────────────────────────────────────


class _LineWriter:
    """Owns the single in-place line.

    Tracks whether a carriage-returned line is currently on screen so permanent
    output is never written on top of a half-drawn one, and pads each render to
    the previous line's width so a shorter line leaves no tail behind.
    """

    def __init__(self, stream=None):
        self._stream = stream
        self._lock = threading.Lock()
        self._last_len = 0
        self._live = False

    @property
    def stream(self):
        # Resolved on each access, not captured at construction, so capsys and
        # sys.stdout.reconfigure() still apply.
        return sys.stdout if self._stream is None else self._stream

    def isatty(self) -> bool:
        try:
            return bool(self.stream.isatty())
        except (AttributeError, ValueError):
            return False

    def render(self, text: str) -> None:
        """Draw the ephemeral line — in place on a TTY, appended when piped."""
        with self._lock:
            stream = self.stream
            if self.isatty():
                width = max(shutil.get_terminal_size(fallback=(80, 24)).columns - 1, 1)
                text = text[:width]
                stream.write("\r" + text + " " * max(self._last_len - len(text), 0))
                self._last_len = len(text)
                self._live = True
            else:
                stream.write(text + "\n")
            stream.flush()

    def clear(self) -> None:
        """Erase the live line so permanent output starts on a clean row."""
        with self._lock:
            if not self._live:
                return
            self.stream.write("\r" + " " * self._last_len + "\r")
            self.stream.flush()
            self._last_len = 0
            self._live = False

    def permanent(self, text: str) -> None:
        """Write a line that stays in the scrollback."""
        self.clear()
        with self._lock:
            self.stream.write(text + "\n")
            self.stream.flush()

    def finish(self) -> None:
        """End the live line, leaving its final frame on screen."""
        with self._lock:
            if not self._live:
                return
            self.stream.write("\n")
            self.stream.flush()
            self._last_len = 0
            self._live = False


# ── Determinate progress ──────────────────────────────────────────────────────


class Progress:
    """Percent, rate and ETA for a loop of known length.

    `start` is the count a resumed run begins at. It counts toward the displayed
    position but not toward the rate or the ETA: crediting a resumed run's
    `start_page * SHIELD_PAGE_SIZE` head start to a few seconds of elapsed time
    reports a rate an order of magnitude too high.

    `unit` is the already-plural noun ("inferences", "batches").

    `stream` and `now` let the tests drive rendering off a fake clock instead of
    sleeping; no caller in these scripts passes them.
    """

    def __init__(
        self,
        total,
        unit,
        prefix="  ",
        start=0,
        stream=None,
        now=time.monotonic,
    ):
        self.total = total or 0
        self.unit = unit
        self.prefix = prefix
        self.start = start
        self.processed = start
        self._now = now
        self._writer = _LineWriter(stream)
        self._started_at = now()
        self._last_render = None
        self._rendered_at = None  # position the last frame showed
        self._closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def update(self, n=1, total=None, suffix=""):
        """Advance by `n`. `total` refreshes the denominator — the Shield page
        count is unknown until the first page comes back."""
        self.processed += n
        if total is not None:
            self.total = total or 0
        complete = bool(self.total) and self.processed >= self.total
        self._render(suffix, force=complete)

    def log(self, message: str) -> None:
        """Emit a permanent line without losing the live one."""
        self._writer.permanent(message)
        self._render(force=True)

    def close(self, summary=None):
        """Terminate the live line. Safe to call more than once."""
        if self._closed:
            return
        self._closed = True
        # A tracker that never advanced (an empty batch, nothing to verify) has
        # nothing worth reporting — don't leave a "0 items" line behind. And if
        # the last frame already showed this position, re-rendering it would
        # just duplicate the final line in a piped log.
        started = self._last_render is not None or self.processed > self.start
        if started and self._rendered_at != self.processed:
            self._render(force=True)
        if summary is None:
            self._writer.finish()
        else:
            self._writer.permanent(self.prefix + summary)

    def _render(self, suffix="", force=False):
        if PROGRESS_INTERVAL <= 0:
            return
        now = self._now()
        if (
            not force
            and self._last_render is not None
            and now - self._last_render < PROGRESS_INTERVAL
        ):
            return
        self._last_render = now
        self._rendered_at = self.processed
        self._writer.render(self._line(suffix))

    def _line(self, suffix=""):
        elapsed = self._now() - self._started_at
        done = self.processed - self.start
        parts = []
        if self.total:
            pct = self.processed / self.total * 100
            parts.append(
                f"[{pct:5.1f}%] {self.processed:,} / {self.total:,} {self.unit}",
            )
        else:
            parts.append(f"{self.processed:,} {self.unit}")
        if done > 0 and elapsed > 0:
            rate = done / elapsed
            parts.append(f"{rate:,.1f}/s")
            remaining = self.total - self.processed
            if self.total and remaining > 0:
                parts.append(f"ETA {format_duration(remaining / rate)}")
        parts.append(f"{format_duration(elapsed)} elapsed")
        if suffix:
            parts.append(suffix)
        return self.prefix + " · ".join(parts)


# ── Indeterminate progress ────────────────────────────────────────────────────


class Heartbeat:
    """Ticks the elapsed time of one blocking call.

    For work with no measurable position — the multi-join COUNT(*)s in
    verify_counts.py and pre_migration_scope.py each run for many minutes with
    no output — this is what distinguishes working from hung.

        with Heartbeat("counting inference_prompt_contents (arthur_shield)"):
            ...

    On the way out it replaces the ticker with a permanent completion line, or
    clears it entirely if the block raised, so a traceback never lands on top of
    a half-drawn line.
    """

    def __init__(self, label, prefix="  ", stream=None, now=time.monotonic):
        self.label = label
        self.prefix = prefix
        self._now = now
        self._writer = _LineWriter(stream)
        self._started_at = None
        self._stop = threading.Event()
        self._thread = None

    @property
    def interval(self):
        if self._writer.isatty():
            return TTY_HEARTBEAT_INTERVAL
        return max(MIN_PIPED_HEARTBEAT_INTERVAL, PROGRESS_INTERVAL)

    def __enter__(self):
        self._started_at = self._now()
        self.tick()
        if PROGRESS_INTERVAL > 0:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        if exc_type is None:
            elapsed = self._now() - self._started_at
            self._writer.permanent(
                f"{self.prefix}✓ {self.label} ({format_duration(elapsed)})",
            )
        else:
            self._writer.clear()
        return False

    def tick(self):
        """Render one frame. Split out from the ticking thread so tests can
        assert on the rendering without running it."""
        if PROGRESS_INTERVAL <= 0 or self._started_at is None:
            return
        elapsed = self._now() - self._started_at
        self._writer.render(f"{self.prefix}{self.label}… {format_duration(elapsed)}")

    def _run(self):
        while not self._stop.wait(self.interval):
            self.tick()
