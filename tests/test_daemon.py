"""Tests for codex-channel-molecule daemon entry point."""

from __future__ import annotations

import argparse
import asyncio
import signal
from unittest.mock import AsyncMock, patch

import pytest

from codex_channel_molecule.daemon import _async_main


@pytest.mark.asyncio
async def test_bridge_crash_is_propagated():
    """If run_bridge raises, the daemon must exit non-zero so systemd restarts."""
    args = argparse.Namespace(codex_bin="codex", turn_timeout_secs=600.0)

    with patch("codex_channel_molecule.daemon.CodexRunner") as mock_runner_cls:
        mock_runner_cls.return_value = AsyncMock()
        with patch(
            "codex_channel_molecule.daemon.run_bridge",
            side_effect=RuntimeError("simulated bridge crash"),
        ):
            with pytest.raises(RuntimeError, match="simulated bridge crash"):
                await _async_main(args)


@pytest.mark.asyncio
async def test_sigint_graceful_shutdown():
    """SIGINT should stop the daemon without raising."""
    args = argparse.Namespace(codex_bin="codex", turn_timeout_secs=600.0)

    async def _fake_run_bridge(*, runner):
        # Simulate the bridge running until signalled.
        await asyncio.sleep(3600)

    with patch("codex_channel_molecule.daemon.CodexRunner") as mock_runner_cls:
        mock_runner_cls.return_value = AsyncMock()
        with patch(
            "codex_channel_molecule.daemon.run_bridge",
            side_effect=_fake_run_bridge,
        ):
            # Schedule a SIGINT after a short delay.
            loop = asyncio.get_running_loop()
            loop.call_later(0.05, lambda: signal.raise_signal(signal.SIGINT))

            result = await _async_main(args)
            assert result == 0
