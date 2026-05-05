"""Spawn ``codex exec`` per inbound message and capture the reply.

``codex exec`` is codex's non-interactive mode — runs one turn and
prints the assistant text to stdout. ``--resume <session-id>`` continues
an existing session if one is given, so chat threads survive across
multiple inbound messages instead of starting fresh every time.

The session id mapping (chat_id → codex session_id) is persisted by
``bridge.py`` — this module only handles spawning a single invocation.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import shutil
from typing import Optional

logger = logging.getLogger(__name__)

# Codex's first turn in a fresh session writes a session_id to stderr
# (``session: <uuid>``). The bridge captures it from CodexResult.session_id
# and stores it for the next inbound on the same chat_id.
_DEFAULT_TIMEOUT_SECS = 600.0  # 10 minutes per turn — same budget as platform A2A


@dataclasses.dataclass(frozen=True)
class CodexResult:
    """Outcome of one ``codex exec`` invocation."""

    text: str
    """Codex's assistant reply, captured from stdout."""

    session_id: Optional[str]
    """The session_id codex used. Same as the input session_id when
    resuming; a freshly-issued uuid when starting a new session. None on
    invocation failure (no session was created)."""

    exit_code: int
    """Process exit code. 0 on success."""

    stderr_tail: str
    """Last ~2 KB of stderr — useful for surfacing codex errors back to
    the caller without dumping the full log."""


class CodexRunner:
    """Wraps ``codex exec`` invocations with session continuity.

    Stateless — the bridge owns the chat_id → session_id map and passes
    the right session_id in each call.
    """

    def __init__(
        self,
        codex_bin: str = "codex",
        timeout_secs: float = _DEFAULT_TIMEOUT_SECS,
    ) -> None:
        # Resolve the codex binary once at construction so a missing
        # install fails fast at daemon start, not on the first inbound.
        resolved = shutil.which(codex_bin)
        if resolved is None:
            raise FileNotFoundError(
                f"codex binary not found on PATH (looked for {codex_bin!r}). "
                f"Install with `npm install -g @openai/codex` or pass "
                f"--codex-bin /path/to/codex."
            )
        self._codex_bin = resolved
        self._timeout_secs = timeout_secs

    async def run(
        self,
        message: str,
        session_id: Optional[str] = None,
    ) -> CodexResult:
        """Run one ``codex exec`` turn with *message* and return the reply.

        When *session_id* is given, ``--resume <session_id>`` continues
        that session. When None, codex starts a fresh session and assigns
        a new id (returned in CodexResult.session_id).
        """
        args: list[str] = [self._codex_bin, "exec", "--skip-git-repo-check"]
        if session_id:
            args.extend(["--resume", session_id])
        args.append(message)

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=os.environ.copy(),
        )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=self._timeout_secs
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return CodexResult(
                text=f"(codex exec timed out after {self._timeout_secs:.0f}s)",
                session_id=session_id,
                exit_code=-1,
                stderr_tail="timeout",
            )

        text = stdout_bytes.decode(errors="replace").strip()
        stderr = stderr_bytes.decode(errors="replace")
        # Cap stderr to keep CodexResult shallow — full stderr is in
        # codex's own log files for postmortems.
        stderr_tail = stderr[-2048:] if len(stderr) > 2048 else stderr

        # Best-effort session_id capture. Codex prints a banner like
        # ``session: 0123abcd-...`` to stderr at session start. When
        # we resumed, session_id is unchanged.
        new_session_id = session_id or _extract_session_id(stderr)

        return CodexResult(
            text=text,
            session_id=new_session_id,
            exit_code=proc.returncode if proc.returncode is not None else -1,
            stderr_tail=stderr_tail,
        )


def _extract_session_id(stderr: str) -> Optional[str]:
    """Pull a session uuid out of codex's stderr banner.

    Codex's exec banner format is fragile across versions, so we accept
    any of the common shapes and return None if nothing matches. The
    daemon falls back to "no session id" gracefully — that just means
    the next turn starts a fresh session, costing context continuity
    but not breaking the message flow.
    """
    import re

    # Two known shapes: "session: <uuid>" and "session_id=<uuid>".
    # Both are stderr-only.
    pattern = re.compile(
        r"session(?:[_ ]id)?[=:\s]+([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        re.IGNORECASE,
    )
    m = pattern.search(stderr)
    return m.group(1) if m else None
