"""Characterize the delta cadence gate that keeps streaming replies cheap.

Every ``assistant_delta`` carries the whole accumulated reply, so the process
re-sanitizes and re-serializes the full text once per upstream chunk. The gate
drops intermediate frames; the wire contract is unchanged because the frames it
does emit still carry the full accumulated text, and the turn always finishes
with the persisted message.
"""

from __future__ import annotations

import pytest

from novelvideo.chat import service


def test_first_frame_is_always_emitted() -> None:
    """A turn that produces one chunk must still show that chunk immediately."""

    gate = service._StreamDeltaGate()

    # Even at an arbitrary base clock reading the first chunk goes out.
    assert gate.allow(now=0.0) is True


def test_frames_inside_the_window_are_dropped() -> None:
    gate = service._StreamDeltaGate()
    interval = service._STREAM_DELTA_MIN_INTERVAL_SECONDS

    assert gate.allow(now=100.0) is True
    assert gate.allow(now=100.0 + interval / 2) is False
    assert gate.allow(now=100.0 + interval * 0.99) is False


def test_a_frame_after_the_window_is_emitted() -> None:
    gate = service._StreamDeltaGate()
    interval = service._STREAM_DELTA_MIN_INTERVAL_SECONDS

    assert gate.allow(now=100.0) is True
    assert gate.allow(now=100.0 + interval * 1.5) is True


def test_gate_restarts_its_window_after_each_emit() -> None:
    """Dropped frames must not accumulate credit and then burst."""

    gate = service._StreamDeltaGate()
    interval = service._STREAM_DELTA_MIN_INTERVAL_SECONDS

    assert gate.allow(now=100.0) is True
    # Ten dropped frames inside one window.
    for offset in range(1, 10):
        assert gate.allow(now=100.0 + interval * offset / 10) is False
    # The window restarts from the last emit, not from the dropped frames.
    assert gate.allow(now=100.0 + interval * 1.5) is True


def test_gate_defaults_to_a_human_readable_frame_rate() -> None:
    """Guard against a window so tight the coalescing does nothing."""

    assert 0.0 < service._STREAM_DELTA_MIN_INTERVAL_SECONDS <= 0.1


@pytest.mark.parametrize("interval", [0.0, 0.01, 0.25])
def test_gate_honours_a_custom_interval(interval: float) -> None:
    gate = service._StreamDeltaGate(interval)

    assert gate.allow(now=50.0) is True
    assert gate.allow(now=50.0 + interval / 2) is (interval == 0.0)
    if interval > 0:
        assert gate.allow(now=50.0 + interval * 2) is True


@pytest.mark.anyio
async def test_coalesced_stream_still_delivers_the_whole_reply() -> None:
    """The frames a gated stream skips must never lose the final text.

    This mirrors the loop in ``_stream_assistant_reply_hermes``: accumulate
    first, then decide whether to pay for sanitizing and emitting. Whatever the
    gate drops, the emitted frames stay full-text prefixes and the persisted
    turn text is complete.
    """

    chunks = [f"第{i}段。" for i in range(60)]
    reply = "".join(chunks)
    gate = service._StreamDeltaGate()
    emitted: list[str] = []
    assistant_text = ""
    clock = 0.0

    for chunk in chunks:
        assistant_text = service._merge_stream_text(assistant_text, chunk)
        # Advance the clock the way real chunks arrive: far faster than the
        # gate window, so most frames are dropped.
        clock += service._STREAM_DELTA_MIN_INTERVAL_SECONDS / 6
        if not gate.allow(now=clock):
            continue
        emitted.append(assistant_text)

    # Coalescing actually happened, but something was still shown while typing.
    assert 1 < len(emitted) < len(chunks)

    # Every emitted frame is the full accumulated text, never a fragment.
    for frame in emitted:
        assert reply.startswith(frame)

    # Emitted frames are strictly growing prefixes: no content goes backwards.
    assert emitted == sorted(emitted, key=len)
    assert len(set(emitted)) == len(emitted)

    # Nothing was lost: the accumulated turn text is the exact reply, which is
    # what ``persist_partial_reply`` stores for the final message.
    assert assistant_text == reply


@pytest.mark.anyio
async def test_gate_still_emits_when_chunks_are_slow() -> None:
    """A slow stream must not be starved into silence."""

    gate = service._StreamDeltaGate()
    interval = service._STREAM_DELTA_MIN_INTERVAL_SECONDS
    emitted = 0
    clock = 0.0

    for _ in range(5):
        clock += interval * 2
        if gate.allow(now=clock):
            emitted += 1

    assert emitted == 5
