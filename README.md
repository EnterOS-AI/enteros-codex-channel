# codex-channel-molecule

Bridge daemon — gives [codex CLI](https://github.com/openai/codex) push parity with the [Molecule AI](https://moleculesai.com) platform's other external runtimes.

The Molecule platform's [`hermes-channel-molecule`](https://git.moleculesai.app/molecule-ai/hermes-channel-molecule) plugin gives `hermes-agent` true push delivery — peer agents and canvas-user messages land mid-session as conversation turns. Codex CLI has no plugin API today and its MCP runtime drops inbound notifications, so this daemon is the equivalent push surface — built outside the codex process.

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

Per chat thread (one canvas-user thread or one peer-workspace thread) gets its own codex session_id, persisted to `~/.codex-channel-molecule/sessions.json` so daemon restarts don't lose conversation context. Set `CODEX_CHANNEL_MOLECULE_STATE_DIR` to override the default location (e.g. when running under systemd with a per-instance state dir).

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

### Running under launchd / systemd (Node-on-PATH note)

The codex binary is a `#!/usr/bin/env node` shim, so spawning it requires `node` to be discoverable on PATH. Under an interactive shell that's automatic; under `launchd` (macOS) and stripped-down `systemd` units PATH defaults to `/usr/bin:/bin`, and `env node` will 127-out silently.

Since 0.1.2 the daemon resolves `codex` to its absolute path and prepends that directory to the subprocess PATH automatically — Node lives next to `codex` under nvm / brew / pnpm-global, so the shim's `env node` finds it. Operators typically don't need any PATH plumbing in their LaunchAgent / unit file beyond the three required env vars.

If you're on an unusual install layout where `codex` and `node` live in different directories, set the LaunchAgent / unit `PATH` explicitly to include both.

## Deprecation path

When [`openai/codex#17543`](https://github.com/openai/codex/issues/17543) lands upstream — a generic path for handling MCP custom notifications in codex and forwarding them into the active session as user submissions — this daemon becomes redundant. Codex itself will accept inbound molecule messages as `Op::UserInput` directly through the MCP server already wired in `~/.codex/config.toml`. Until then, this is the operator-facing answer.

## Development

```sh
git clone https://git.moleculesai.app/molecule-ai/codex-channel-molecule.git
cd codex-channel-molecule
pip install -e ".[test]"
pytest -q
```

Tests are entirely real-subprocess (no mocking the spawn boundary) so the boot path is covered the same way the daemon runs in production.

## Releasing

Tag-on-push triggers `publish.yml` which builds + publishes to PyPI via OIDC trusted publishing (no API token needed).

```sh
# Bump pyproject.toml `version`, commit, then:
git tag v0.1.1 && git push origin v0.1.1
```

The workflow refuses to publish if the tag doesn't match `pyproject.toml`'s `version` — keeps PyPI versions and git tags in lockstep.

**One-time PyPI setup** (before the first release):

1. Create the project on PyPI by uploading the first wheel manually, OR
2. Pre-register the project on PyPI under a "Pending publisher" config so the first tagged push creates it.

Either way, on the project's PyPI page → "Manage" → "Publishing" → "Add a new publisher", configure:

- Owner: `Molecule-AI`
- Repository: `codex-channel-molecule`
- Workflow filename: `publish.yml`
- Environment name: `pypi`

After this, every `git push origin v*.*.*` ships the wheel to PyPI without any further intervention.

## License

Apache-2.0
