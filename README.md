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

The Codex tab wires the Molecule MCP server into `~/.codex/config.toml` so Codex can call platform tools (`list_peers`, `delegate_task`, `send_message_to_user`, `commit_memory`, etc.). That's outbound — Codex calls out to the platform.

This daemon is the inbound counterpart: it long-polls the platform inbox and turns each message into a Codex turn. Run both for full bidirectional integration.

## Install

```sh
npm install -g @openai/codex@latest

PRIVATE_WHEELS="$(mktemp -d)"
python3 -m pip download --no-deps --dest "$PRIVATE_WHEELS" \
  --index-url https://git.moleculesai.app/api/packages/molecule-ai/pypi/simple/ \
  molecules-workspace-runtime==0.4.26 \
  codex-channel-molecule==0.1.4
python3 -m pip install --index-url https://pypi.org/simple/ \
  "$PRIVATE_WHEELS"/molecules_workspace_runtime-*.whl \
  "$PRIVATE_WHEELS"/codex_channel_molecule-*.whl
rm -rf "$PRIVATE_WHEELS"
```

Both Molecule-owned wheels come from the internal Gitea registry. That registry
does not proxy public dependencies, so the install deliberately downloads the
private wheels without dependencies and then lets public PyPI resolve only the
dependencies of those local artifacts. Do not replace this with an unrestricted
`--extra-index-url`; that makes package selection ambiguous across registries.

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

[`openai/codex#17543`](https://github.com/openai/codex/issues/17543) remains open. When Codex gains that generic path for injecting MCP custom notifications into an active session, this daemon can become redundant; until then, it provides the supported inbound bridge.

## Development

```sh
git clone https://git.moleculesai.app/molecule-ai/codex-channel-molecule.git
cd codex-channel-molecule

RUNTIME_WHEEL="$(mktemp -d)"
python3 -m pip download --no-deps --dest "$RUNTIME_WHEEL" \
  --index-url https://git.moleculesai.app/api/packages/molecule-ai/pypi/simple/ \
  molecules-workspace-runtime==0.4.26
python3 -m pip install --index-url https://pypi.org/simple/ \
  "$RUNTIME_WHEEL"/molecules_workspace_runtime-*.whl \
  pytest pytest-asyncio
rm -rf "$RUNTIME_WHEEL"
python3 -m pip install --no-deps -e .
pytest -q
```

Tests are entirely real-subprocess (no mocking the spawn boundary) so the boot path is covered the same way the daemon runs in production.

## Releasing

Releases are tag-driven and publish to the Molecule AI Gitea package registry.
Routine development still lands through reviewed PRs; after the version bump is
merged to `main`, push the matching tag:

```sh
# For the version currently declared in pyproject.toml:
git tag v0.1.4 && git push origin v0.1.4
```

`.gitea/workflows/publish.yml` refuses a tag that does not match
`pyproject.toml`, builds and smoke-tests the wheel, publishes only this
project's wheel and sdist, then verifies an anonymous clean install from the
Gitea registry.

### Publisher credential

The publish job reads the least-privilege `MOL_PACKAGE_TOKEN` from Infisical
(`prod /ci/gitea-pypi-publisher`) at runtime. It fails closed when the machine
identity or secret is absent. There is no operator host and no local credential
bundle in this release path.

## License

Apache-2.0
