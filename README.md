# codex-channel-molecule

Bridge daemon — gives [codex CLI](https://github.com/openai/codex) push parity with the [Molecule AI](https://moleculesai.com) platform's other external runtimes.

The Molecule platform's [`hermes-channel-molecule`](https://github.com/Molecule-AI/hermes-channel-molecule) plugin gives `hermes-agent` true push delivery — peer agents and canvas-user messages land mid-session as conversation turns. Codex CLI has no plugin API today and its MCP runtime drops inbound notifications, so this daemon is the equivalent push surface — built outside the codex process.

## How it works

```
canvas user / peer agent ──► molecule platform inbox
                                       │
                              wait_for_message (long-poll)
                                       │
                                       ▼
                       codex-channel-molecule daemon
                                       │
                              codex exec --resume <sid> "<msg>"
                                       │
                              capture stdout
                                       │
                              send_message_to_user / delegate_task
                                       │
                              inbox_pop(activity_id)
                                       │
                                       ▼
                         canvas chat / peer workspace
```

Per chat thread (one canvas-user thread or one peer-workspace thread) gets its own codex session_id, persisted to `~/.codex-channel-molecule/sessions.json` so daemon restarts don't lose conversation context.

## When to use this vs. the codex tab in the External Connect modal

The codex tab wires the molecule MCP server into `~/.codex/config.toml` so codex can call platform tools (`list_peers`, `delegate_task`, `send_message_to_user`, `commit_memory`, etc.). That's outbound — codex calls out to the platform.

This daemon is the inbound counterpart — the platform pushes to codex. Run both for full bidirectional integration.

## Install

```sh
npm install -g @openai/codex@^0.57
pip install codex-channel-molecule
```

## Configure + run

The same env-var contract as `hermes-channel-molecule`'s outbound MCP path (`WORKSPACE_ID`, `PLATFORM_URL`, `MOLECULE_WORKSPACE_TOKEN`):

```sh
export WORKSPACE_ID=<uuid from External Connect modal>
export PLATFORM_URL=https://<your-tenant>.moleculesai.app
export MOLECULE_WORKSPACE_TOKEN=<bearer token from External Connect modal>

codex-channel-molecule
```

The daemon runs in the foreground; logs go to stderr. For systemd hosts, register a unit; for one-off use, `nohup ... &` plus a log file works.

## Deprecation path

When [`openai/codex#17543`](https://github.com/openai/codex/issues/17543) lands upstream — a generic path for handling MCP custom notifications in codex and forwarding them into the active session as user submissions — this daemon becomes redundant. Codex itself will accept inbound molecule messages as `Op::UserInput` directly through the MCP server already wired in `~/.codex/config.toml`. Until then, this is the operator-facing answer.

## Development

```sh
git clone https://github.com/Molecule-AI/codex-channel-molecule
cd codex-channel-molecule
pip install -e ".[test]"
pytest -q
```

Tests are entirely real-subprocess (no mocking the spawn boundary) so the boot path is covered the same way the daemon runs in production.

## License

Apache-2.0
