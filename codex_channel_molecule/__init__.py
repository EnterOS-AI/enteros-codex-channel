"""codex-channel-molecule — bridge daemon for codex CLI ↔ Molecule platform.

The platform's ``hermes-channel-molecule`` plugin gives hermes-agent push
parity (peer messages and canvas chat arrive mid-session as conversation
turns) by hooking the hermes platform-plugin API. Codex CLI has no
plugin API today and its MCP runtime drops inbound notifications, so
this daemon is the equivalent push surface — built outside the codex
process.

Loop:
    1. Long-poll ``molecule_runtime.a2a_tools.tool_wait_for_message``
    2. On a real message, run ``codex exec --resume <session_id> "<text>"``
    3. Capture codex's stdout, route back via ``tool_send_message_to_user``
       (canvas) or ``tool_delegate_task`` (peer)
    4. Ack with ``tool_inbox_pop(activity_id)``

Session continuity is per chat_id: the daemon keeps a small JSON map of
``chat_id → codex session_id`` so each peer or canvas-user thread
resumes its own codex session across messages.

When `openai/codex#17543` lands upstream, this daemon becomes
redundant — codex itself will accept MCP notifications as
``Op::UserInput``. Until then, this is the operator-facing answer.
"""

from .bridge import run_bridge
from .codex_runner import CodexRunner, CodexResult

__all__ = ["run_bridge", "CodexRunner", "CodexResult"]
