"""Console-script entry point — ``codex-channel-molecule``.

Validates the env-var contract (mirrors hermes-channel-molecule), wires
up signal handling, and runs the bridge loop until interrupted.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys

from .bridge import run_bridge
from .codex_runner import CodexRunner

_REQUIRED_ENV = ("WORKSPACE_ID", "PLATFORM_URL", "MOLECULE_WORKSPACE_TOKEN")


def _check_env() -> list[str]:
    missing = [name for name in _REQUIRED_ENV if not os.environ.get(name, "").strip()]
    return missing


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="codex-channel-molecule",
        description=(
            "Bridge daemon — long-polls the molecule platform inbox and "
            "runs `codex exec --resume <session>` per inbound message. "
            "Reply routes back via send_message_to_user (canvas) or "
            "delegate_task (peer). Per-thread codex session continuity "
            "is persisted to disk."
        ),
    )
    parser.add_argument(
        "--codex-bin",
        default="codex",
        help="codex CLI binary name or absolute path (default: codex).",
    )
    parser.add_argument(
        "--turn-timeout-secs",
        type=float,
        default=600.0,
        help="Per-turn ceiling on `codex exec` runtime (default: 600).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="Logging level (default: INFO).",
    )
    return parser.parse_args(argv)


async def _async_main(args: argparse.Namespace) -> int:
    runner = CodexRunner(
        codex_bin=args.codex_bin,
        timeout_secs=args.turn_timeout_secs,
    )

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _request_stop() -> None:
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _request_stop)

    bridge_task = asyncio.create_task(run_bridge(runner=runner))
    stop_task = asyncio.create_task(stop_event.wait())

    done, pending = await asyncio.wait(
        {bridge_task, stop_task},
        return_when=asyncio.FIRST_COMPLETED,
    )
    for t in pending:
        t.cancel()
    for t in pending:
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass

    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    missing = _check_env()
    if missing:
        sys.stderr.write(
            "codex-channel-molecule: required env vars not set: "
            + ", ".join(missing)
            + "\n  WORKSPACE_ID — UUID of the workspace this daemon represents\n"
            + "  PLATFORM_URL — https://<your-tenant>.moleculesai.app\n"
            + "  MOLECULE_WORKSPACE_TOKEN — bearer token from the platform's\n"
            + "    External Connect modal for that workspace\n"
        )
        return 2

    try:
        return asyncio.run(_async_main(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
