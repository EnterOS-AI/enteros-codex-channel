"""Inbox poll → codex turn → reply route → ack loop.

This is the only loop in the daemon. It calls into the molecule_runtime
tool implementations directly (no MCP-server subprocess in the middle —
this daemon IS the codex-side caller).

Session continuity: each chat thread (one peer workspace, or one
canvas-user thread) gets its own codex session_id. The mapping is
persisted to ``$CODEX_CHANNEL_MOLECULE_STATE_DIR/sessions.json`` (default
``~/.codex-channel-molecule/sessions.json``) so daemon restarts don't
lose conversation context.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional, Protocol

from .codex_runner import CodexRunner, CodexResult

logger = logging.getLogger(__name__)

# Long-poll budget. molecule_runtime's wait_for_message caps at 300s
# server-side; pick something below that so we don't hit the platform's
# read timeout. 60s is the same default the MCP tool advertises.
_LONG_POLL_SECS = 60.0

# Exponential backoff bounds for wait_for_message errors. Doubles from
# _BACKOFF_INITIAL up to _BACKOFF_MAX after each consecutive failure;
# resets to zero on the first successful poll. Without this, a stale
# token / DNS failure / dead platform turns into a tight 1-Hz retry
# storm — bad citizenship across N operator daemons.
_BACKOFF_INITIAL = 1.0
_BACKOFF_MAX = 60.0


class _Tools(Protocol):
    """Minimal interface the bridge needs from molecule_runtime.

    Defined as a Protocol so tests can supply a fake without touching
    real platform endpoints.
    """

    async def wait_for_message(self, timeout_secs: float) -> str: ...
    async def inbox_pop(self, activity_id: str) -> str: ...
    async def send_message_to_user(
        self, message: str, workspace_id: Optional[str]
    ) -> str: ...
    async def delegate_task(
        self, workspace_id: str, task: str, source_workspace_id: Optional[str]
    ) -> str: ...


class _RealTools:
    """Production implementation — calls into the installed
    molecule-ai-workspace-runtime wheel.

    The wheel is imported lazily on first instantiation so unit tests
    that supply a fake `_Tools` don't require it.
    """

    def __init__(self) -> None:
        from molecule_runtime import a2a_tools

        self._tools = a2a_tools

    async def wait_for_message(self, timeout_secs: float) -> str:
        return await self._tools.tool_wait_for_message(timeout_secs=timeout_secs)

    async def inbox_pop(self, activity_id: str) -> str:
        return await self._tools.tool_inbox_pop(activity_id=activity_id)

    async def send_message_to_user(
        self, message: str, workspace_id: Optional[str]
    ) -> str:
        return await self._tools.tool_send_message_to_user(
            message=message, workspace_id=workspace_id
        )

    async def delegate_task(
        self, workspace_id: str, task: str, source_workspace_id: Optional[str]
    ) -> str:
        return await self._tools.tool_delegate_task(
            workspace_id=workspace_id,
            task=task,
            source_workspace_id=source_workspace_id,
        )


class _SessionStore:
    """chat_id → codex session_id, persisted to disk.

    Atomic write via tmp-then-rename so a crash mid-write doesn't
    corrupt the map.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._sessions: Dict[str, str] = {}
        if path.exists():
            try:
                self._sessions = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning(
                    "session store at %s is unreadable (%s) — starting empty",
                    path, exc,
                )

    def get(self, chat_id: str) -> Optional[str]:
        return self._sessions.get(chat_id)

    def set(self, chat_id: str, session_id: str) -> None:
        if self._sessions.get(chat_id) == session_id:
            return
        self._sessions[chat_id] = session_id
        self._flush()

    def _flush(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._sessions, indent=2, sort_keys=True))
        tmp.replace(self._path)


def _state_dir() -> Path:
    explicit = os.environ.get("CODEX_CHANNEL_MOLECULE_STATE_DIR", "").strip()
    if explicit:
        return Path(explicit)
    return Path.home() / ".codex-channel-molecule"


def _chat_id_for(message: Dict[str, Any]) -> str:
    """Stable per-thread key for session continuity.

    canvas_user → ``canvas:<workspace_id>`` (one session per workspace's
    canvas chat — same as hermes-channel's encoding).
    peer_agent  → ``peer:<peer_workspace_id>`` (one session per peer).

    Fallback ``unknown:<activity_id>`` for malformed messages — gives
    the message its own one-off session rather than poisoning shared
    state.
    """
    kind = message.get("kind", "")
    if kind == "canvas_user":
        ws = message.get("arrival_workspace_id") or message.get("workspace_id") or ""
        return f"canvas:{ws}"
    if kind == "peer_agent":
        peer = message.get("peer_id", "")
        return f"peer:{peer}"
    return f"unknown:{message.get('activity_id', '')}"


def _extract_inbox_message(payload: str) -> Optional[Dict[str, Any]]:
    """Decode the wait_for_message JSON-string return value.

    Returns None for the timeout sentinel ``{"timeout": true}`` and any
    payload that doesn't look like an inbox row.
    """
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("timeout"):
        return None
    if "activity_id" not in data:
        return None
    return data


async def _route_reply(
    tools: _Tools,
    message: Dict[str, Any],
    reply_text: str,
) -> None:
    """Send codex's output back to the right destination."""
    kind = message.get("kind", "")
    if kind == "canvas_user":
        ws = message.get("arrival_workspace_id") or message.get("workspace_id")
        await tools.send_message_to_user(message=reply_text, workspace_id=ws)
    elif kind == "peer_agent":
        peer = message.get("peer_id", "")
        if not peer:
            logger.warning("peer_agent message lacks peer_id, dropping reply")
            return
        await tools.delegate_task(
            workspace_id=peer,
            task=reply_text,
            source_workspace_id=message.get("arrival_workspace_id"),
        )
    else:
        logger.warning("unknown message kind %r, dropping reply", kind)


async def run_bridge(
    *,
    runner: CodexRunner,
    tools: Optional[_Tools] = None,
    session_store: Optional[_SessionStore] = None,
    iterations: Optional[int] = None,
) -> None:
    """Main bridge loop.

    *iterations*: when set (test path), break after N inbound-or-timeout
    cycles. Default None means run forever.
    """
    tools = tools or _RealTools()
    session_store = session_store or _SessionStore(_state_dir() / "sessions.json")

    cycle = 0
    backoff = 0.0
    while iterations is None or cycle < iterations:
        cycle += 1
        try:
            payload = await tools.wait_for_message(timeout_secs=_LONG_POLL_SECS)
        except Exception as exc:
            backoff = min(backoff * 2, _BACKOFF_MAX) if backoff else _BACKOFF_INITIAL
            logger.warning(
                "wait_for_message raised: %s — backing off %.1fs", exc, backoff,
            )
            await asyncio.sleep(backoff)
            continue
        backoff = 0.0

        message = _extract_inbox_message(payload)
        if message is None:
            continue

        await _handle_one(message, runner=runner, tools=tools, store=session_store)


async def _handle_one(
    message: Dict[str, Any],
    *,
    runner: CodexRunner,
    tools: _Tools,
    store: _SessionStore,
) -> None:
    """Process a single inbound message, then ack."""
    activity_id = message.get("activity_id", "")
    chat_id = _chat_id_for(message)
    session_id = store.get(chat_id)
    body = _message_body(message)

    logger.info(
        "inbound %s (chat=%s session=%s) — %d chars",
        activity_id, chat_id, session_id or "new", len(body),
    )

    result: CodexResult = await runner.run(message=body, session_id=session_id)
    if result.session_id and result.session_id != session_id:
        store.set(chat_id, result.session_id)

    reply = result.text or "(codex returned empty output)"
    if result.exit_code != 0:
        reply = f"{reply}\n\n(codex exec failed: exit={result.exit_code})"

    try:
        await _route_reply(tools, message, reply)
    except Exception as exc:
        # Don't ack if we couldn't deliver — let the next poll cycle
        # retry. Inbox is at-least-once delivery; the platform will
        # re-surface the activity.
        logger.error("reply routing failed for %s: %s", activity_id, exc)
        return

    try:
        await tools.inbox_pop(activity_id=activity_id)
    except Exception as exc:
        logger.warning("inbox_pop(%s) failed: %s", activity_id, exc)


def _message_body(message: Dict[str, Any]) -> str:
    """Extract the inbound text. Format mirrors the MCP wait_for_message
    response: a 'text' field at top level, OR a 'parts' list of dicts
    with 'text' fields (A2A multipart shape)."""
    text = message.get("text")
    if isinstance(text, str) and text:
        return text
    parts = message.get("parts")
    if isinstance(parts, list):
        chunks = []
        for p in parts:
            if isinstance(p, dict) and isinstance(p.get("text"), str):
                chunks.append(p["text"])
        if chunks:
            return "\n".join(chunks)
    return ""
