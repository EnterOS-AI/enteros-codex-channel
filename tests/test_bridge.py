"""Bridge-loop tests with fake tools + fake codex runner.

The fakes capture every call so each test asserts exact contracts:
which kind of message reaches which reply tool, what activity_ids are
acked, how session_id continuity is maintained across turns.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from codex_channel_molecule.bridge import _SessionStore, run_bridge
from codex_channel_molecule.codex_runner import CodexResult


# ----------------------------------------------------------------------
# Fakes
# ----------------------------------------------------------------------


class FakeTools:
    """Records every tool call. wait_for_message replays from a queue
    seeded by the test."""

    def __init__(self, inbox: List[Dict[str, Any]]) -> None:
        self._inbox: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()
        for m in inbox:
            self._inbox.put_nowait(m)
        self.popped: List[str] = []
        self.canvas_replies: List[Tuple[str, Optional[str]]] = []  # (text, ws)
        self.peer_replies: List[Tuple[str, str, Optional[str]]] = []
        # (workspace_id, task, source_workspace_id)

    async def wait_for_message(self, timeout_secs: float) -> str:
        # Drain the queue immediately; a real implementation would block
        # for timeout_secs. After exhaustion, return the timeout sentinel
        # so the bridge keeps cycling without hanging.
        try:
            msg = self._inbox.get_nowait()
            return json.dumps(msg)
        except asyncio.QueueEmpty:
            return json.dumps({"timeout": True, "timeout_secs": timeout_secs})

    async def inbox_pop(self, activity_id: str) -> str:
        self.popped.append(activity_id)
        return json.dumps({"removed": True, "activity_id": activity_id})

    async def send_message_to_user(
        self, message: str, workspace_id: Optional[str]
    ) -> str:
        self.canvas_replies.append((message, workspace_id))
        return "ok"

    async def delegate_task(
        self, workspace_id: str, task: str, source_workspace_id: Optional[str]
    ) -> str:
        self.peer_replies.append((workspace_id, task, source_workspace_id))
        return "ok"


class FakeRunner:
    """Returns scripted CodexResults; records every call. Lets tests
    pin session-continuity behavior without spawning real codex."""

    def __init__(self, results: List[CodexResult]) -> None:
        self._results = list(results)
        self.calls: List[Tuple[str, Optional[str]]] = []  # (message, session_id)

    async def run(
        self, message: str, session_id: Optional[str] = None
    ) -> CodexResult:
        self.calls.append((message, session_id))
        if not self._results:
            return CodexResult(
                text="(no scripted result)",
                session_id=session_id,
                exit_code=0,
                stderr_tail="",
            )
        return self._results.pop(0)


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_canvas_user_message_is_dispatched_acked_and_replied(tmp_path):
    """Canvas-user inbound → CodexRunner.run → send_message_to_user →
    inbox_pop. Assert the full chain in one go."""
    inbox = [{
        "kind": "canvas_user",
        "activity_id": "act-1",
        "arrival_workspace_id": "ws-canvas",
        "text": "hi can you help",
    }]
    tools = FakeTools(inbox)
    runner = FakeRunner([CodexResult(
        text="sure, what's up",
        session_id="sess-canvas-1",
        exit_code=0,
        stderr_tail="",
    )])
    store = _SessionStore(tmp_path / "sessions.json")

    await run_bridge(runner=runner, tools=tools, session_store=store, iterations=2)

    assert runner.calls == [("hi can you help", None)]
    assert tools.canvas_replies == [("sure, what's up", "ws-canvas")]
    assert tools.peer_replies == []
    assert tools.popped == ["act-1"]
    assert store.get("canvas:ws-canvas") == "sess-canvas-1"


@pytest.mark.asyncio
async def test_peer_agent_message_routes_to_delegate_task(tmp_path):
    inbox = [{
        "kind": "peer_agent",
        "activity_id": "act-2",
        "peer_id": "ws-peer",
        "arrival_workspace_id": "ws-self",
        "text": "what's your status",
    }]
    tools = FakeTools(inbox)
    runner = FakeRunner([CodexResult(
        text="all good", session_id="sess-peer-1", exit_code=0, stderr_tail="",
    )])
    store = _SessionStore(tmp_path / "sessions.json")

    await run_bridge(runner=runner, tools=tools, session_store=store, iterations=2)

    assert tools.peer_replies == [("ws-peer", "all good", "ws-self")]
    assert tools.canvas_replies == []
    assert tools.popped == ["act-2"]
    assert store.get("peer:ws-peer") == "sess-peer-1"


@pytest.mark.asyncio
async def test_session_continuity_resumes_same_codex_session(tmp_path):
    """Two messages on the same chat_id → second call resumes the
    session_id captured from the first."""
    inbox = [
        {"kind": "canvas_user", "activity_id": "act-a",
         "arrival_workspace_id": "ws-x", "text": "first"},
        {"kind": "canvas_user", "activity_id": "act-b",
         "arrival_workspace_id": "ws-x", "text": "second"},
    ]
    tools = FakeTools(inbox)
    runner = FakeRunner([
        CodexResult(text="r1", session_id="sess-shared", exit_code=0, stderr_tail=""),
        CodexResult(text="r2", session_id="sess-shared", exit_code=0, stderr_tail=""),
    ])
    store = _SessionStore(tmp_path / "sessions.json")

    await run_bridge(runner=runner, tools=tools, session_store=store, iterations=3)

    # First call: no session_id (new). Second call: resume sess-shared.
    assert runner.calls == [("first", None), ("second", "sess-shared")]
    assert tools.popped == ["act-a", "act-b"]


@pytest.mark.asyncio
async def test_session_store_persists_across_runs(tmp_path):
    """Session map survives daemon restart — written to disk on each
    update, re-read on the next instantiation."""
    state_file = tmp_path / "sessions.json"
    inbox_one = [{
        "kind": "canvas_user", "activity_id": "act-1",
        "arrival_workspace_id": "ws-resume", "text": "first ever",
    }]
    tools_one = FakeTools(inbox_one)
    runner_one = FakeRunner([CodexResult(
        text="hi", session_id="sess-persist", exit_code=0, stderr_tail="",
    )])
    store_one = _SessionStore(state_file)
    await run_bridge(
        runner=runner_one, tools=tools_one, session_store=store_one, iterations=2,
    )
    assert state_file.exists()

    # Simulate daemon restart — fresh store reads the same file.
    inbox_two = [{
        "kind": "canvas_user", "activity_id": "act-2",
        "arrival_workspace_id": "ws-resume", "text": "follow up",
    }]
    tools_two = FakeTools(inbox_two)
    runner_two = FakeRunner([CodexResult(
        text="ok", session_id="sess-persist", exit_code=0, stderr_tail="",
    )])
    store_two = _SessionStore(state_file)
    await run_bridge(
        runner=runner_two, tools=tools_two, session_store=store_two, iterations=2,
    )

    # Second instance must have resumed the persisted session id.
    assert runner_two.calls == [("follow up", "sess-persist")]


@pytest.mark.asyncio
async def test_timeout_sentinel_does_not_call_codex(tmp_path):
    """An empty inbox returns a timeout sentinel — the bridge must
    keep polling without spawning codex."""
    tools = FakeTools(inbox=[])  # queue empty → timeout sentinel
    runner = FakeRunner(results=[])
    store = _SessionStore(tmp_path / "sessions.json")

    await run_bridge(runner=runner, tools=tools, session_store=store, iterations=3)

    assert runner.calls == []
    assert tools.popped == []
    assert tools.canvas_replies == []
    assert tools.peer_replies == []


@pytest.mark.asyncio
async def test_failed_reply_routing_skips_inbox_pop(tmp_path):
    """If sending the reply fails, do NOT ack the inbox row — the
    platform will re-deliver on the next poll. At-least-once semantics.
    """
    inbox = [{
        "kind": "canvas_user", "activity_id": "act-err",
        "arrival_workspace_id": "ws-x", "text": "msg",
    }]

    class FlakyTools(FakeTools):
        async def send_message_to_user(self, message, workspace_id):
            raise RuntimeError("simulated 502 from platform")

    tools = FlakyTools(inbox)
    runner = FakeRunner([CodexResult(
        text="reply", session_id="sess", exit_code=0, stderr_tail="",
    )])
    store = _SessionStore(tmp_path / "sessions.json")

    await run_bridge(runner=runner, tools=tools, session_store=store, iterations=2)

    assert runner.calls == [("msg", None)]
    # Must NOT have popped — at-least-once requires the unacked row to
    # re-surface next poll.
    assert tools.popped == []


@pytest.mark.asyncio
async def test_nonzero_exit_code_surfaces_in_reply(tmp_path):
    """Codex failure (e.g. timeout, crash) becomes a visible reply
    instead of silently dropping. Operator sees the failure where the
    answer was expected."""
    inbox = [{
        "kind": "canvas_user", "activity_id": "act-fail",
        "arrival_workspace_id": "ws-x", "text": "ping",
    }]
    tools = FakeTools(inbox)
    runner = FakeRunner([CodexResult(
        text="(codex exec timed out after 600s)",
        session_id=None,
        exit_code=-1,
        stderr_tail="timeout",
    )])
    store = _SessionStore(tmp_path / "sessions.json")

    await run_bridge(runner=runner, tools=tools, session_store=store, iterations=2)

    assert len(tools.canvas_replies) == 1
    text, _ = tools.canvas_replies[0]
    assert "timed out" in text
    assert "exit=-1" in text
    # The row is acked — codex's verdict (success or failure) is
    # delivered, so the inbox row is fully handled.
    assert tools.popped == ["act-fail"]


@pytest.mark.asyncio
async def test_a2a_multipart_text_is_concatenated(tmp_path):
    """A2A messages can arrive as ``parts: [{type: text, text: ...}, ...]``
    instead of a flat ``text`` field. Bridge concatenates parts into a
    single codex prompt."""
    inbox = [{
        "kind": "peer_agent", "activity_id": "act-p", "peer_id": "ws-peer",
        "parts": [
            {"type": "text", "text": "first chunk"},
            {"type": "text", "text": "second chunk"},
        ],
    }]
    tools = FakeTools(inbox)
    runner = FakeRunner([CodexResult(
        text="ack", session_id="s", exit_code=0, stderr_tail="",
    )])
    store = _SessionStore(tmp_path / "sessions.json")

    await run_bridge(runner=runner, tools=tools, session_store=store, iterations=2)

    msg, _ = runner.calls[0]
    assert msg == "first chunk\nsecond chunk"
