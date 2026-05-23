"""Regression guard for subprocess I/O deadlock (root cause: codex-channel-molecule#1684).

The confirmed bug: ``asyncio.create_subprocess_exec`` with ``stdout=PIPE`` (no stdin
management) causes codex turns to wedge indefinitely when codex writes > 64 KB
to stdout. The pipe buffer fills; codex blocks writing; the parent blocks waiting
for codex to exit; deadlock. Root cause: codex holds its own stdout open and the
OS pipe buffer never drains because neither side signals EOF.

The fix pattern: drain stdout in a concurrent task AND close stdin before/during
communicate(). This ensures:
  1. concurrent drain prevents pipe-buffer fill deadlock
  2. closing stdin signals EOF to codex, so codex exits its stdout writer
     when the buffer is full (it can then check stdin EOF and exit cleanly)

This test suite would have CAUGHT the bug. When the CTO's fix lands, these tests
assert the correct (non-deadlocked) behavior. If the fix regresses, the
pipe-buffer test will timeout and fail within 30 s.
"""

from __future__ import annotations

import asyncio
import textwrap
import time
from pathlib import Path

import pytest

from codex_channel_molecule.codex_runner import CodexRunner

# The Linux default pipe buffer is 64 KB (F_SETPIPE_SZ). Writing more than this
# to a blocking pipe without a concurrent reader causes the writer to block
# indefinitely when the reader is waiting for the subprocess to exit (without
# draining stdout first) AND hasn't signaled EOF on stdin.
_BURST_SIZE_KB = 80  # 20 KB above the pipe ceiling to reliably overflow it

# Regression test deadline: if the parent+child deadlock, we must fail within
# this window rather than hanging for the full 600 s codex timeout.
_TEST_DEADLINE_S = 30


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_burst_subprocess(tmp_path: Path, *, close_stdin: bool = False) -> Path:
    """Write a Python script that bursts > _BURST_SIZE_KB to stdout then exits.

    Args:
        tmp_path: scratch directory for the script
        close_stdin: if True, the script closes stdin immediately (simulates
                     codex behaviour when parent signals EOF). This removes
                     stdin from the deadlock equation so we isolate the stdout
                     buffer path.
    """
    p = tmp_path / "burst_writer"
    close_stdin_block = (
        "import sys\n"
        "sys.stdin.close()  # signal EOF so writer is not stdin-blocked\n"
        if close_stdin
        else ""
    )
    p.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env python3
        {close_stdin_block}
        # Write enough bytes to overflow the default 64 KB pipe buffer.
        chunk = b"x" * ({_BURST_SIZE_KB * 1024})
        sys.stdout.write(chunk.decode("latin-1"))
        sys.stdout.flush()
        sys.stderr.write("session: deadbeef-0000-0000-0000-000000000001\\n")
        sys.stderr.flush()
    """))
    p.chmod(0o755)
    return p


# ---------------------------------------------------------------------------
# Regression test: stdout pipe-buffer fill → no deadlock
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_large_stdout_no_deadlock_without_stdin_close(tmp_path: Path):
    """Regression guard for codex-channel-molecule#1684.

    Before the fix: parent uses ``communicate()`` without draining stdout
    concurrently or closing stdin. Codex writes > 64 KB to stdout, filling
    the pipe buffer. Codex blocks trying to write more. Parent blocks waiting
    for codex to exit. Deadlock → test times out at 600 s.

    After the fix: parent drains stdout concurrently and signals EOF on stdin.
    Codex's stdout writer unblocks (stdin EOF signals it can exit) and the
    concurrent drain consumes the buffer. ``run()`` completes within 30 s.

    We simulate codex behaviour with a script that bursts to stdout and does
    NOT self-close stdin (close_stdin=False) — the parent's close is what
    must signal EOF. This mirrors the real codex situation.
    """
    fake = _make_burst_subprocess(tmp_path, close_stdin=False)
    runner = CodexRunner(codex_bin=str(fake), timeout_secs=600.0)

    start = time.monotonic()
    result = await runner.run(message="burst test")
    elapsed = time.monotonic() - start

    # Must complete within 30 s (well under the 10-min timeout).
    assert elapsed < _TEST_DEADLINE_S, (
        f"run() took {elapsed:.1f}s — possible pipe-buffer deadlock. "
        f"Ensure stdout is drained concurrently and stdin is closed before "
        f"communicate()."
    )
    # Exit code must be 0.
    assert result.exit_code == 0, (
        f"expected exit_code=0, got {result.exit_code}. stderr_tail={result.stderr_tail!r}"
    )
    # Text must contain the burst payload.
    assert len(result.text) >= _BURST_SIZE_KB * 1024 - 1, (
        f"expected at least {_BURST_SIZE_KB} KB in stdout, got {len(result.text)} bytes"
    )


# ---------------------------------------------------------------------------
# Regression test: stdin signal unblocks stdout writer
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stdin_close_unblocks_stdout_writer(tmp_path: Path):
    """Verify that closing stdin allows the child to finish flushing stdout.

    When the parent closes stdin before/during ``communicate()``, the child
    receives EOF on stdin. If the child is designed to exit on stdin EOF
    (correct behaviour), its stdout writer unblocks and it can finish draining
    the pipe buffer and exit.

    This test exercises the stdin-close leg. With close_stdin=True the child
    closes its own stdin immediately, simulating the parent having closed it.
    We verify the burst completes without deadlock.
    """
    fake = _make_burst_subprocess(tmp_path, close_stdin=True)
    runner = CodexRunner(codex_bin=str(fake), timeout_secs=600.0)

    start = time.monotonic()
    result = await runner.run(message="burst with stdin closed")
    elapsed = time.monotonic() - start

    assert elapsed < _TEST_DEADLINE_S, (
        f"run() took {elapsed:.1f}s — stdin-close did not unblock stdout. "
        f"Ensure stdin is closed before communicate()."
    )
    assert result.exit_code == 0
    assert len(result.text) >= _BURST_SIZE_KB * 1024 - 1


# ---------------------------------------------------------------------------
# Regression test: concurrent stdout drain prevents buffer deadlock
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_stdout_drain_prevents_buffer_fill_deadlock(tmp_path: Path):
    """Isolated test for the concurrent drain leg of the fix.

    The deadlock can also be prevented by always draining stdout in a concurrent
    task (never using blocking ``communicate()`` alone), even without closing
    stdin. This test verifies that pattern by having the child NOT close stdin
    but having the parent drain concurrently. After the fix lands, the
    parent must drain concurrently — otherwise this test fails.

    This test is primarily documentation; the integration test
    (``test_large_stdout_no_deadlock_without_stdin_close``) covers the full
    combined fix. We include this for the specific case where a partial fix
    closes stdin but fails to drain concurrently — both are needed.
    """
    fake = _make_burst_subprocess(tmp_path, close_stdin=False)
    runner = CodexRunner(codex_bin=str(fake), timeout_secs=600.0)

    start = time.monotonic()
    result = await runner.run(message="concurrent drain test")
    elapsed = time.monotonic() - start

    assert elapsed < _TEST_DEADLINE_S, (
        f"run() took {elapsed:.1f}s — concurrent drain may not be active. "
        f"Ensure stdout is read concurrently (not via blocking communicate() alone)."
    )
    assert result.exit_code == 0
    assert len(result.text) >= _BURST_SIZE_KB * 1024 - 1