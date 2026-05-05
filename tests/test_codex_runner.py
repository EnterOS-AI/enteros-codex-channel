"""Real-subprocess test for CodexRunner — boot path coverage.

In-process mocking would miss subprocess-level bugs (env handling,
arg passthrough, stderr capture, signal/timeout). The fake script is a
real Python program spawned via asyncio.create_subprocess_exec, exactly
as a real codex install would be.
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

from codex_channel_molecule.codex_runner import (
    CodexRunner,
    _extract_session_id,
)


_FAKE_CODEX_SCRIPT = textwrap.dedent("""\
    #!/usr/bin/env python3
    \"\"\"Fake codex CLI for tests.

    Behaviors keyed on argv shape and env:
      argv: codex exec [--skip-git-repo-check] [--resume <sid>] <message>

    Echoes a banner to stderr (\"session: <uuid>\") and the input message
    to stdout. Honors FAKE_EXIT_CODE for failure-path tests.
    \"\"\"
    import os, sys

    args = sys.argv[1:]
    assert args[0] == \"exec\", f\"unexpected first arg: {args[0]!r}\"

    resume_id = None
    i = 1
    while i < len(args):
        if args[i] == \"--resume\":
            resume_id = args[i + 1]
            i += 2
        elif args[i].startswith(\"--\"):
            i += 1  # skip unrecognized flag
        else:
            break
    msg = args[i] if i < len(args) else \"\"

    sid = resume_id or os.environ.get(\"FAKE_NEW_SESSION_ID\", \"a1b2c3d4-1111-2222-3333-444455556666\")
    sys.stderr.write(f\"session: {sid}\\n\")
    sys.stderr.flush()

    sys.stdout.write(f\"echo: {msg}\\n\")
    sys.stdout.flush()

    sys.exit(int(os.environ.get(\"FAKE_EXIT_CODE\", \"0\")))
""")


@pytest.fixture
def fake_codex(tmp_path: Path) -> Path:
    p = tmp_path / "codex"
    p.write_text(_FAKE_CODEX_SCRIPT)
    p.chmod(0o755)
    return p


@pytest.mark.asyncio
async def test_run_returns_stdout_text(fake_codex):
    runner = CodexRunner(codex_bin=str(fake_codex), timeout_secs=10.0)
    result = await runner.run(message="hello world")
    assert result.text == "echo: hello world"
    assert result.exit_code == 0


@pytest.mark.asyncio
async def test_run_extracts_new_session_id_from_stderr(fake_codex, monkeypatch):
    runner = CodexRunner(codex_bin=str(fake_codex), timeout_secs=10.0)
    monkeypatch.setenv("FAKE_NEW_SESSION_ID", "deadbeef-0000-1111-2222-333344445555")
    result = await runner.run(message="any")
    assert result.session_id == "deadbeef-0000-1111-2222-333344445555"


@pytest.mark.asyncio
async def test_run_resumes_existing_session(fake_codex):
    runner = CodexRunner(codex_bin=str(fake_codex), timeout_secs=10.0)
    given = "11111111-2222-3333-4444-555555555555"
    result = await runner.run(message="follow up", session_id=given)
    # Resume → no new session id is captured (input is echoed back as-is).
    assert result.session_id == given


@pytest.mark.asyncio
async def test_run_surfaces_nonzero_exit_code(fake_codex, monkeypatch):
    runner = CodexRunner(codex_bin=str(fake_codex), timeout_secs=10.0)
    monkeypatch.setenv("FAKE_EXIT_CODE", "7")
    result = await runner.run(message="any")
    assert result.exit_code == 7


@pytest.mark.asyncio
async def test_run_kills_subprocess_on_timeout(tmp_path):
    """A codex turn that hangs past the timeout must be killed and
    surfaced as a timeout result — not block the bridge forever."""
    sleeper = tmp_path / "codex"
    sleeper.write_text(textwrap.dedent("""\
        #!/usr/bin/env python3
        import time
        time.sleep(60)
    """))
    sleeper.chmod(0o755)

    runner = CodexRunner(codex_bin=str(sleeper), timeout_secs=0.5)
    result = await runner.run(message="hang please")
    assert "timed out" in result.text
    assert result.exit_code == -1


def test_constructor_fails_fast_when_codex_missing():
    with pytest.raises(FileNotFoundError) as excinfo:
        CodexRunner(codex_bin="/nonexistent/path/to/codex-binary-xyzzy")
    assert "codex" in str(excinfo.value).lower()


def test_extract_session_id_matches_canonical_banner():
    assert _extract_session_id("session: a1b2c3d4-aaaa-bbbb-cccc-dddddddddddd\n") == \
        "a1b2c3d4-aaaa-bbbb-cccc-dddddddddddd"


def test_extract_session_id_matches_alternate_banner_shape():
    assert _extract_session_id(
        "blah blah\nsession_id=12345678-aaaa-bbbb-cccc-dddddddddddd\nmore\n"
    ) == "12345678-aaaa-bbbb-cccc-dddddddddddd"


def test_extract_session_id_returns_none_on_no_match():
    assert _extract_session_id("nothing relevant here") is None
