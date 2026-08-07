"""Tests for shield_migration_scripts/progress.py."""

import progress
import pytest
from progress import Heartbeat, Progress, format_duration


@pytest.fixture(autouse=True)
def default_interval(monkeypatch):
    """Pin the throttle so tests don't depend on MIGRATION_PROGRESS_INTERVAL."""
    monkeypatch.setattr(progress, "PROGRESS_INTERVAL", 2.0)


# ── format_duration ───────────────────────────────────────────────────────────


@pytest.mark.unit_tests
@pytest.mark.parametrize(
    "seconds,expected",
    [
        (0.0, "0.0s"),
        (0.5, "0.5s"),
        (59.9, "59.9s"),
        (60, "1m 00s"),
        (249, "4m 09s"),
        (3723, "1h 02m 03s"),
    ],
)
def test_format_duration(seconds, expected):
    assert format_duration(seconds) == expected


# ── Rendering ─────────────────────────────────────────────────────────────────


@pytest.mark.unit_tests
def test_tty_rewrites_one_line_in_place(tty_stream, fake_clock):
    tracker = Progress(100, "rows", stream=tty_stream, now=fake_clock)
    for _ in range(3):
        fake_clock.advance(5)
        tracker.update()

    output = tty_stream.getvalue()
    assert output.count("\r") == 3
    # Nothing is committed to scrollback until the tracker is closed.
    assert "\n" not in output

    tracker.close()
    assert tty_stream.getvalue().endswith("\n")


@pytest.mark.unit_tests
def test_piped_appends_whole_lines_and_never_uses_carriage_returns(
    piped_stream,
    fake_clock,
):
    tracker = Progress(100, "rows", stream=piped_stream, now=fake_clock)
    for _ in range(3):
        fake_clock.advance(5)
        tracker.update()
    tracker.close()

    output = piped_stream.getvalue()
    assert "\r" not in output
    assert output.count("\n") == 3
    assert all(line.strip() for line in output.splitlines())


@pytest.mark.unit_tests
def test_close_is_idempotent(tty_stream, fake_clock):
    tracker = Progress(10, "rows", stream=tty_stream, now=fake_clock)
    fake_clock.advance(1)
    tracker.update()
    tracker.close()
    before = tty_stream.getvalue()
    tracker.close()
    assert tty_stream.getvalue() == before


# ── Throttling ────────────────────────────────────────────────────────────────


@pytest.mark.unit_tests
def test_updates_are_throttled_not_one_line_per_call(piped_stream, fake_clock):
    """The regression this module exists for: at ~1B inferences the old
    per-page print emitted 200,000 lines."""
    tracker = Progress(10_000, "inferences", stream=piped_stream, now=fake_clock)
    for _ in range(1_000):
        fake_clock.advance(0.001)  # 1s of work in total
        tracker.update()
    tracker.close()

    # 1s elapsed at a 2s interval: the opening frame plus the closing one.
    assert len(piped_stream.frames) == 2


@pytest.mark.unit_tests
def test_reaching_the_total_always_renders_even_mid_throttle(
    piped_stream,
    fake_clock,
):
    tracker = Progress(2, "rows", stream=piped_stream, now=fake_clock)
    fake_clock.advance(5)
    tracker.update()  # renders — first frame
    fake_clock.advance(0.001)  # well inside the throttle window
    tracker.update()  # completes the work, so must still render

    assert len(piped_stream.frames) == 2
    assert "100.0%" in piped_stream.frames[-1]


@pytest.mark.unit_tests
def test_interval_zero_disables_the_live_line(piped_stream, fake_clock, monkeypatch):
    monkeypatch.setattr(progress, "PROGRESS_INTERVAL", 0)
    tracker = Progress(10, "rows", stream=piped_stream, now=fake_clock)
    for _ in range(10):
        fake_clock.advance(60)
        tracker.update()
    assert piped_stream.getvalue() == ""

    # A summary is permanent output, not a live frame, so it still prints.
    tracker.close(summary="done")
    assert "done" in piped_stream.getvalue()


# ── Percent, rate and ETA ─────────────────────────────────────────────────────


@pytest.mark.unit_tests
def test_percent_rate_and_eta(piped_stream, fake_clock):
    tracker = Progress(1_000, "inferences", stream=piped_stream, now=fake_clock)
    fake_clock.advance(10)
    tracker.update(400)

    line = piped_stream.frames[-1]
    assert "[ 40.0%]" in line
    assert "400 / 1,000 inferences" in line
    assert "40.0/s" in line
    assert "ETA 15.0s" in line  # 600 remaining at 40/s
    assert "10.0s elapsed" in line


@pytest.mark.unit_tests
def test_resume_offset_is_excluded_from_the_rate(piped_stream, fake_clock):
    """A resumed run starts at start_page * SHIELD_PAGE_SIZE. Crediting that
    head start to this run's elapsed time reports a wildly inflated rate."""
    tracker = Progress(
        1_000,
        "inferences",
        start=900,
        stream=piped_stream,
        now=fake_clock,
    )
    fake_clock.advance(10)
    tracker.update(50)

    line = piped_stream.frames[-1]
    assert "950 / 1,000 inferences" in line  # position includes the offset
    assert "5.0/s" in line  # 50 records in 10s, not 95/s
    assert "95.0/s" not in line
    assert "ETA 10.0s" in line


@pytest.mark.unit_tests
def test_unknown_total_renders_without_percent_or_eta(piped_stream, fake_clock):
    """Shield's count is 0 until the first page comes back."""
    tracker = Progress(0, "inferences", stream=piped_stream, now=fake_clock)
    fake_clock.advance(1)
    tracker.update(10)

    line = piped_stream.frames[-1]
    assert "10 inferences" in line
    assert "%" not in line
    assert "ETA" not in line


@pytest.mark.unit_tests
def test_total_can_arrive_after_the_first_update(piped_stream, fake_clock):
    tracker = Progress(0, "inferences", stream=piped_stream, now=fake_clock)
    fake_clock.advance(5)
    tracker.update(10)
    assert "%" not in piped_stream.frames[-1]

    fake_clock.advance(5)
    tracker.update(10, total=100)
    assert "[ 20.0%]" in piped_stream.frames[-1]


@pytest.mark.unit_tests
def test_zero_total_and_zero_elapsed_do_not_divide_by_zero(piped_stream, fake_clock):
    tracker = Progress(0, "rows", stream=piped_stream, now=fake_clock)
    tracker.update(0)  # no time passed, nothing processed
    tracker.close()


@pytest.mark.unit_tests
def test_a_tracker_that_never_advanced_stays_silent(piped_stream, fake_clock):
    """Nothing to verify, nothing to delete — don't leave a '0 items' line."""
    tracker = Progress(0, "task-less inferences", stream=piped_stream, now=fake_clock)
    tracker.close()
    assert piped_stream.getvalue() == ""


# ── Terminal width ────────────────────────────────────────────────────────────


@pytest.mark.unit_tests
def test_lines_are_truncated_to_the_terminal_width(tty_stream, fake_clock, monkeypatch):
    monkeypatch.setenv("COLUMNS", "40")
    monkeypatch.setenv("LINES", "24")
    tracker = Progress(
        1_000_000,
        "inferences with a very long unit label",
        stream=tty_stream,
        now=fake_clock,
    )
    fake_clock.advance(10)
    tracker.update(123_456)

    for frame in tty_stream.frames:
        assert len(frame) <= 39


@pytest.mark.unit_tests
def test_a_shorter_frame_erases_the_tail_of_a_longer_one(tty_stream, fake_clock):
    tracker = Progress(0, "rows", stream=tty_stream, now=fake_clock)
    fake_clock.advance(5)
    tracker.update(1_000_000, suffix="a long trailing note")
    long_len = len(tty_stream.getvalue().split("\r")[-1])

    fake_clock.advance(5)
    tracker.update(0, suffix="")  # explicitly cleared, so the frame shrinks
    short_frame = tty_stream.getvalue().split("\r")[-1]
    # Padded out to the previous width so no tail survives.
    assert len(short_frame) == long_len
    assert short_frame.endswith(" ")


# ── Permanent output and exceptions ───────────────────────────────────────────


@pytest.mark.unit_tests
def test_log_writes_a_permanent_line_and_redraws(tty_stream, fake_clock):
    tracker = Progress(100, "rows", stream=tty_stream, now=fake_clock)
    fake_clock.advance(5)
    tracker.update()
    tracker.log("  something worth keeping")

    output = tty_stream.getvalue()
    assert "something worth keeping\n" in output
    # The live line is redrawn after the permanent one.
    assert output.rindex("\r") > output.rindex("something worth keeping")


@pytest.mark.unit_tests
def test_log_redraw_keeps_the_last_suffix(piped_stream, fake_clock):
    """A redraw after permanent output must not silently drop the suffix — it
    carries the per-run detail (jobs running, rows inserted)."""
    tracker = Progress(100, "rows", stream=piped_stream, now=fake_clock)
    fake_clock.advance(5)
    tracker.update(1, suffix="5 running")
    tracker.log("[t1] linked")

    assert "5 running" in piped_stream.frames[-1]


@pytest.mark.unit_tests
def test_close_keeps_the_last_suffix(piped_stream, fake_clock):
    tracker = Progress(100, "rows", stream=piped_stream, now=fake_clock)
    fake_clock.advance(5)
    tracker.update(1, suffix="5 running")
    fake_clock.advance(5)
    tracker.update(1)  # no suffix supplied
    assert "5 running" in piped_stream.frames[-1]


@pytest.mark.unit_tests
def test_exception_inside_the_context_terminates_the_line(tty_stream, fake_clock):
    with pytest.raises(ValueError):
        with Progress(100, "rows", stream=tty_stream, now=fake_clock) as tracker:
            fake_clock.advance(5)
            tracker.update()
            raise ValueError("boom")

    # A traceback must not be glued onto a half-drawn line.
    assert tty_stream.getvalue().endswith("\n")


@pytest.mark.unit_tests
def test_close_with_summary_replaces_the_live_line(tty_stream, fake_clock):
    tracker = Progress(100, "rows", prefix="  ", stream=tty_stream, now=fake_clock)
    fake_clock.advance(5)
    tracker.update()
    tracker.close(summary="42 rows migrated")

    assert tty_stream.getvalue().endswith("  42 rows migrated\n")


# ── Heartbeat ─────────────────────────────────────────────────────────────────


@pytest.mark.unit_tests
def test_heartbeat_tick_renders_the_label_and_elapsed(tty_stream, fake_clock):
    beat = Heartbeat("counting inferences", stream=tty_stream, now=fake_clock)
    with beat:
        fake_clock.advance(252)
        beat.tick()

    frames = tty_stream.frames
    assert frames[0] == "  counting inferences… 0.0s"
    assert "  counting inferences… 4m 12s" in frames
    assert frames[-1] == "  ✓ counting inferences (4m 12s)"


@pytest.mark.unit_tests
def test_heartbeat_clears_without_a_checkmark_when_the_block_raises(
    tty_stream,
    fake_clock,
):
    with pytest.raises(RuntimeError):
        with Heartbeat("counting inferences", stream=tty_stream, now=fake_clock):
            fake_clock.advance(3)
            raise RuntimeError("query failed")

    output = tty_stream.getvalue()
    assert "✓" not in output
    # The line is erased so the traceback starts clean.
    assert output.endswith("\r")


@pytest.mark.unit_tests
def test_heartbeat_ticks_slowly_when_piped(piped_stream, tty_stream, fake_clock):
    """A once-a-second tick would add ~1,200 lines to a logfile for a single
    20-minute query."""
    piped = Heartbeat("counting", stream=piped_stream, now=fake_clock)
    tty = Heartbeat("counting", stream=tty_stream, now=fake_clock)

    assert tty.interval == progress.TTY_HEARTBEAT_INTERVAL
    assert piped.interval >= progress.MIN_PIPED_HEARTBEAT_INTERVAL


@pytest.mark.unit_tests
def test_heartbeat_interval_zero_still_reports_completion(piped_stream, monkeypatch):
    monkeypatch.setattr(progress, "PROGRESS_INTERVAL", 0)
    clock = iter([0.0, 5.0]).__next__
    with Heartbeat("counting inferences", stream=piped_stream, now=clock):
        pass

    output = piped_stream.getvalue()
    assert "counting inferences… " not in output  # no ticking
    assert output.strip() == "✓ counting inferences (5.0s)"


@pytest.mark.unit_tests
def test_heartbeat_thread_stops_on_exit(tty_stream, fake_clock):
    beat = Heartbeat("counting", stream=tty_stream, now=fake_clock)
    with beat:
        assert beat._thread is not None and beat._thread.is_alive()
    assert not beat._thread.is_alive()
