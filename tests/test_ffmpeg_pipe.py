from __future__ import annotations

import io

from vpstitch.ffmpegio import _BoundedPipeReader


def test_bounded_pipe_reader_drains_and_retains_recent_output() -> None:
    payload = b"old\n" * 100_000 + b"final diagnostic\n"
    reader = _BoundedPipeReader(io.BytesIO(payload), max_bytes=32 * 1024)
    text = reader.finish()

    assert text.endswith("final diagnostic\n")
    assert len(text.encode("utf-8")) <= 32 * 1024
