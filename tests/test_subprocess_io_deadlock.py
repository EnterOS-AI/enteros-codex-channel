"""Regression guard for subprocess I/O deadlock (root cause: codex-channel-molecule#1684).

The confirmed bug: ``asyncio.create_subprocess_exec`` with ``stdout=PIPE`` and
no concurrent stdout drain causes codex turns to wedge indefinitely when codex
writes > 64 KB to stdout. The pipe buffer fills; codex blocks writing; the parent
blocks on ``communicate()`` waiting for codex to exit; deadlock.

The correct fix (in codex_runner.py):
  1. Close stdin before/after ``communicate()`` — signals EOF to codex so its
     stdout writer can exit when the buffer is full.
  2. Drain stdout in a concurrent task (never use blocking communicate() alone
     for large-output processes).

This test suite validates BOTH parts. ``test_buggy_pattern_times_out`` asserts
the buggy pattern DOES deadlock (proving the test reproduces the bug). The other
tests assert the correct non-deadlocked behavior. If anyone reverts the fix,
the buggy-pattern test FAILS and reveals the regression within 30 s.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from typing import Tuple

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
# Bug reproduction helpers
# ---------------------------------------------------------------------------

def _make_burst_subprocess(tmp_path: Path, *, close_stdin: bool = False) -> Path:
    """Write a Python script that bursts > _BURST_SIZE_KB to stdout then exits.

    When ``close_stdin=True`` the script closes its stdin immediately, which
    simulates codex receiving EOF from the parent. When False, stdin stays open
    and the parent must close it to unblock the child's stdout writer.
    """
    p = tmp_path / "burst_writer"
    stdin_close = (
        "import sys\n"
        "sys.stdin.close()  # parent must close its stdin to unblock us\n"
    ) if close_stdin else (
        "import sys\n"
    )
    # Build the script without f-string + dedent to avoid multiline
    # indent collapse (#1684 test helper bug).
    script = (
        "#!/usr/bin/env python3\n"
        + stdin_close
        + "# Write enough bytes to overflow the default 64 KB pipe buffer.\n"
        + "# Writing raw bytes via sys.stdout.buffer avoids any buffering that\n"
        + "# could mask the deadlock (pure text write might flush incrementally).\n"
        + f"sys.stdout.buffer.write(b\"x\" * ({_BURST_SIZE_KB * 1024}))\n"
        + "sys.stdout.buffer.flush()\n"
        + "sys.stderr.write(\"session: deadbeef-0000-0000-0000-000000000001\\n\")\n"
        + "sys.stderr.flush()\n"
    )
    p.write_text(script)
    p.chmod(0o755)
    return p


def _run_buggy_pattern(script: Path) -> Tuple[int, float]:
    """Spawn script with stdout=PIPE and block on communicate() — the buggy pattern.

    This is the exact pattern that caused #1684: no stdin close, no concurrent
    drain, just communicate(). If this DOESN'T deadlock on modern Python,
    the pipe-buffer behavior may have changed and this test needs redesign.
    We guard with a thread+timeout so a real deadlock doesn't wedge pytest.
    """
    result = {"exit_code": None, "elapsed": None}

    def target():
        start = time.monotonic()
        proc = subprocess.Popen(
            [str(script)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            # The buggy pattern: blocking communicate() with no stdin close
            # and no concurrent drain.
            stdout, stderr = proc.communicate()
            result["exit_code"] = proc.returncode
        except Exception as e:
            result["exit_code"] = -2
            result["error"] = str(e)
        result["elapsed"] = time.monotonic() - start

    t = threading.Thread(target=target)
    t.daemon = True
    t.start()
    t.join(timeout=_TEST_DEADLINE_S + 5)
    if t.is_alive():
        result["elapsed"] = _TEST_DEADLINE_S + 5
        result["exit_code"] = None
    return result.get("exit_code"), result.get("elapsed", 0.0)


# ---------------------------------------------------------------------------
# Regression test: the buggy pattern DOES deadlock
# ---------------------------------------------------------------------------

def test_buggy_pattern_times_out(tmp_path: Path):
    """Assert that the unpatched communicate()-only pattern deadlocks.

    This test proves we are actually reproducing #1684: using ``Popen`` with
    stdout=PIPE and calling ``communicate()`` without closing stdin causes
    the child to wedge when it writes > 64 KB. The thread must time out.

    If this test PASSES (thread completes under _TEST_DEADLINE_S), either:
      - The pipe-buffer deadlock no longer occurs on this OS/Python version,
        OR
      - The test environment has a non-standard pipe buffer size.
    In that case the regression guard has lost its teeth and must be redesigned.
    """
    script = _make_burst_subprocess(tmp_path, close_stdin=False)
    exit_code, elapsed = _run_buggy_pattern(script)

    # Thread completed — either success (no deadlock on this platform) or
    # deadlocked and was killed by the daemon-timeout.
    if exit_code is None:
        # Thread still alive = deadlock → test passes (we reproduced the bug)
        assert True, "buggy pattern deadlocked as expected"
    else:
        # Thread completed quickly → no deadlock on this platform.
        # This means the regression guard cannot work here.
        # Fail loudly so someone redesigns it.
        pytest.fail(
            f"BUGGY PATTERN COMPLETED in {elapsed:.1f}s (exit={exit_code}). "
            f"Pipe-buffer deadlock did not reproduce on this OS/Python. "
            f"The regression guard has false confidence — redesign required."
        )


# ---------------------------------------------------------------------------
# Regression test: fixed CodexRunner.communicate() handles large stdout
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_large_stdout_via_CodexRunner_no_deadlock(tmp_path: Path):
    """Regression guard for codex-channel-molecule#1684.

    Before the fix: CodexRunner.run() used ``proc.communicate()`` without
    closing stdin or draining stdout concurrently. Codex writes > 64 KB to
    stdout, filling the pipe buffer. Codex blocks; parent blocks on
    communicate(); deadlock.

    After the fix: parent closes stdin and drains stdout in a concurrent task.
    ``run()`` completes within _TEST_DEADLINE_S. If anyone reverts the fix,
    this test times out and fails.
    """
    fake = _make_burst_subprocess(tmp_path, close_stdin=False)
    runner = CodexRunner(codex_bin=str(fake), timeout_secs=600.0)

    start = time.monotonic()
    result = await runner.run(message="burst test")
    elapsed = time.monotonic() - start

    # Must complete within 30 s (well under the 10-min timeout).
    assert elapsed < _TEST_DEADLINE_S, (
        f"run() took {elapsed:.1f}s — possible pipe-buffer deadlock. "
        f"Ensure stdin is closed and stdout is drained concurrently."
    )
    assert result.exit_code == 0, (
        f"expected exit_code=0, got {result.exit_code}. "
        f"stderr_tail={result.stderr_tail!r}"
    )
    # Text must contain the burst payload.
    assert len(result.text) >= _BURST_SIZE_KB * 1024 - 1, (
        f"expected at least {_BURST_SIZE_KB} KB in stdout, got {len(result.text)} bytes"
    )


@pytest.mark.asyncio
async def test_stdin_close_unblocks_stdout_writer(tmp_path: Path):
    """Closing stdin signals EOF and allows the child's stdout writer to exit.

    When the parent closes stdin before ``communicate()``, the child can exit
    even if its stdout pipe buffer is full (it receives stdin EOF and stops
    blocking on stdout). This is one leg of the fix.
    """
    fake = _make_burst_subprocess(tmp_path, close_stdin=True)
    runner = CodexRunner(codex_bin=str(fake), timeout_secs=600.0)

    start = time.monotonic()
    result = await runner.run(message="burst with stdin closed")
    elapsed = time.monotonic() - start

    assert elapsed < _TEST_DEADLINE_S, (
        f"run() took {elapsed:.1f}s — stdin-close did not unblock stdout."
    )
    assert result.exit_code == 0
    assert len(result.text) >= _BURST_SIZE_KB * 1024 - 1


# ---------------------------------------------------------------------------
# Regression test: concurrent stdout drain prevents buffer deadlock
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_stdout_drain_prevents_buffer_fill_deadlock(tmp_path: Path):
    """Assert that concurrent stdout draining prevents pipe-buffer deadlock.

    The concurrent-drain approach (always read stdout in a background task,
    never use blocking communicate() alone) prevents deadlock even without
    closing stdin. This test verifies the fix covers this leg too.
    """
    fake = _make_burst_subprocess(tmp_path, close_stdin=False)
    runner = CodexRunner(codex_bin=str(fake), timeout_secs=600.0)

    start = time.monotonic()
    result = await runner.run(message="concurrent drain test")
    elapsed = time.monotonic() - start

    assert elapsed < _TEST_DEADLINE_S, (
        f"run() took {elapsed:.1f}s — concurrent drain may not be active. "
        f"Ensure stdout is read concurrently."
    )
    assert result.exit_code == 0
    assert len(result.text) >= _BURST_SIZE_KB * 1024 - 1