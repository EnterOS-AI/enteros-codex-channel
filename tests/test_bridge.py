"""Bridge-loop tests with fake tools + fake codex runner.

The fakes capture every call so each test asserts exact contracts:
which kind of message reaches which reply tool, what activity_ids are
acked, how session_id continuity is maintained across turns.
"""

from __future__ import annotations

import asyncio
import json
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


# ----------------------------------------------------------------------
# Error-path coverage — branches operators actually hit in production
# when the platform is flaky, the message is malformed, or codex returns
# nothing useful. Each fake supplies the exact failure shape.
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wait_for_message_raises_triggers_exponential_backoff(tmp_path):
    """When wait_for_message raises (DNS failure, dead platform, expired
    token), the bridge must back off exponentially so N daemons under a
    common outage don't melt the platform with 1 Hz retries.

    Sleep is monkeypatched to a recorder so the test runs instantly while
    still asserting the exact 1, 2, 4, 8, 16, 32, 60-cap progression.
    """
    class AlwaysRaisingTools(FakeTools):
        async def wait_for_message(self, timeout_secs: float) -> str:
            raise ConnectionError("simulated platform outage")

    tools = AlwaysRaisingTools(inbox=[])
    runner = FakeRunner(results=[])
    store = _SessionStore(tmp_path / "sessions.json")

    sleeps: List[float] = []

    async def _record_sleep(secs):
        sleeps.append(secs)

    import codex_channel_molecule.bridge as bridge_mod

    orig_sleep = bridge_mod.asyncio.sleep
    bridge_mod.asyncio.sleep = _record_sleep
    try:
        await run_bridge(runner=runner, tools=tools, session_store=store, iterations=8)
    finally:
        bridge_mod.asyncio.sleep = orig_sleep

    # Expected progression: 1 → 2 → 4 → 8 → 16 → 32 → 60 (cap) → 60.
    assert sleeps == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]
    assert runner.calls == []  # never reached codex


@pytest.mark.asyncio
async def test_backoff_resets_on_first_successful_poll(tmp_path):
    """A success after consecutive failures must clear the backoff so
    the next failure starts at 1s again — not stuck at the cap."""
    sequence: List[Any] = ["raise", "raise", {
        "kind": "canvas_user", "activity_id": "act-1",
        "arrival_workspace_id": "ws-x", "text": "real",
    }, "raise"]

    class StepTools(FakeTools):
        def __init__(self):
            super().__init__(inbox=[])
            self._idx = 0

        async def wait_for_message(self, timeout_secs: float) -> str:
            item = sequence[self._idx]
            self._idx += 1
            if item == "raise":
                raise ConnectionError("flake")
            return json.dumps(item)

    tools = StepTools()
    runner = FakeRunner([CodexResult(
        text="ok", session_id="sess", exit_code=0, stderr_tail="",
    )])
    store = _SessionStore(tmp_path / "sessions.json")

    sleeps: List[float] = []

    async def _record_sleep(secs):
        sleeps.append(secs)

    import codex_channel_molecule.bridge as bridge_mod

    orig_sleep = bridge_mod.asyncio.sleep
    bridge_mod.asyncio.sleep = _record_sleep
    try:
        await run_bridge(runner=runner, tools=tools, session_store=store, iterations=4)
    finally:
        bridge_mod.asyncio.sleep = orig_sleep

    # 1s after first raise, 2s after second, success in between resets,
    # then 1s again after the trailing raise.
    assert sleeps == [1.0, 2.0, 1.0]


@pytest.mark.asyncio
async def test_inbox_pop_failure_logs_but_does_not_re_run_codex(tmp_path):
    """If pop fails after a successful reply, codex must NOT run again
    on the same message — the platform still has the row but our turn
    is done. We accept duplicate delivery on the next poll cycle."""
    inbox = [{
        "kind": "canvas_user", "activity_id": "act-pop-fail",
        "arrival_workspace_id": "ws-x", "text": "msg",
    }]

    class PopFailTools(FakeTools):
        async def inbox_pop(self, activity_id):
            raise RuntimeError("simulated 503")

    tools = PopFailTools(inbox)
    runner = FakeRunner([CodexResult(
        text="reply", session_id="sess", exit_code=0, stderr_tail="",
    )])
    store = _SessionStore(tmp_path / "sessions.json")

    await run_bridge(runner=runner, tools=tools, session_store=store, iterations=2)

    # Reply was sent; codex ran exactly once even though pop blew up.
    assert runner.calls == [("msg", None)]
    assert tools.canvas_replies == [("reply", "ws-x")]


@pytest.mark.asyncio
async def test_peer_agent_without_peer_id_drops_reply_and_acks(tmp_path):
    """A malformed peer_agent message (no peer_id — registry lookup
    failure or platform bug) must not crash the bridge or call
    delegate_task with an empty workspace_id.

    The row IS acked: keeping an undeliverable message in the queue
    would loop forever on every poll. Operator sees the warning in the
    log and can investigate — at-least-once means "we won't lose a
    message we CAN deliver", not "we'll loop on poison forever".
    """
    inbox = [{
        "kind": "peer_agent", "activity_id": "act-no-peer",
        "text": "from a peer with no id",
    }]
    tools = FakeTools(inbox)
    runner = FakeRunner([CodexResult(
        text="reply", session_id="sess", exit_code=0, stderr_tail="",
    )])
    store = _SessionStore(tmp_path / "sessions.json")

    await run_bridge(runner=runner, tools=tools, session_store=store, iterations=2)

    assert runner.calls == [("from a peer with no id", None)]
    assert tools.peer_replies == []  # no destination → no delegate_task call
    assert tools.canvas_replies == []
    assert tools.popped == ["act-no-peer"]  # poison drained


@pytest.mark.asyncio
async def test_unknown_kind_drops_reply_and_acks(tmp_path):
    """Future or malformed message kinds must not crash the daemon —
    log + drop + ack so the queue isn't blocked. A future daemon that
    learns the new kind reads it from the platform's persistent log,
    not from the inbox."""
    inbox = [{
        "kind": "future_kind_we_dont_know", "activity_id": "act-?",
        "text": "??",
    }]
    tools = FakeTools(inbox)
    runner = FakeRunner([CodexResult(
        text="r", session_id="s", exit_code=0, stderr_tail="",
    )])
    store = _SessionStore(tmp_path / "sessions.json")

    await run_bridge(runner=runner, tools=tools, session_store=store, iterations=2)

    assert tools.canvas_replies == []
    assert tools.peer_replies == []
    assert tools.popped == ["act-?"]  # drained, not looped


@pytest.mark.asyncio
async def test_empty_codex_output_falls_back_to_placeholder(tmp_path):
    """If codex returns empty stdout (silent failure, model refused, etc.)
    we still send SOMETHING to the user — silent inbound never gets a
    reply and looks like the daemon ate it."""
    inbox = [{
        "kind": "canvas_user", "activity_id": "act-silent",
        "arrival_workspace_id": "ws-x", "text": "hi",
    }]
    tools = FakeTools(inbox)
    runner = FakeRunner([CodexResult(
        text="", session_id="sess", exit_code=0, stderr_tail="",
    )])
    store = _SessionStore(tmp_path / "sessions.json")

    await run_bridge(runner=runner, tools=tools, session_store=store, iterations=2)

    assert len(tools.canvas_replies) == 1
    text, _ = tools.canvas_replies[0]
    assert text == "(codex returned empty output)"
    assert tools.popped == ["act-silent"]


@pytest.mark.asyncio
async def test_canvas_user_falls_back_to_workspace_id(tmp_path):
    """The chat-id encoder accepts ``workspace_id`` when
    ``arrival_workspace_id`` is absent — older platform builds use the
    short field name. Same session keying either way."""
    inbox = [{
        "kind": "canvas_user", "activity_id": "act-old-shape",
        "workspace_id": "ws-legacy",  # no arrival_workspace_id
        "text": "msg",
    }]
    tools = FakeTools(inbox)
    runner = FakeRunner([CodexResult(
        text="ack", session_id="sess-legacy", exit_code=0, stderr_tail="",
    )])
    store = _SessionStore(tmp_path / "sessions.json")

    await run_bridge(runner=runner, tools=tools, session_store=store, iterations=2)

    assert tools.canvas_replies == [("ack", "ws-legacy")]
    assert store.get("canvas:ws-legacy") == "sess-legacy"


@pytest.mark.parametrize(
    "payload",
    [
        "not json at all",                  # JSONDecodeError
        '"a string, not an object"',         # decoded but not a dict
        '{"timeout": true}',                 # real timeout sentinel
        '{"no_activity_id": "field"}',        # dict but missing activity_id
    ],
)
@pytest.mark.asyncio
async def test_malformed_payloads_are_silently_dropped(tmp_path, payload):
    """Each shape that wait_for_message can return must skip the message
    handler without raising. Invariant comes from _extract_inbox_message."""
    class StaticTools(FakeTools):
        def __init__(self, payload):
            super().__init__(inbox=[])
            self._payload = payload

        async def wait_for_message(self, timeout_secs):
            return self._payload

    tools = StaticTools(payload)
    runner = FakeRunner(results=[])
    store = _SessionStore(tmp_path / "sessions.json")

    await run_bridge(runner=runner, tools=tools, session_store=store, iterations=2)

    assert runner.calls == []
    assert tools.popped == []
