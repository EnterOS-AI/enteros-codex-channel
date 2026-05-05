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
      argv: codex exec [--skip-git-repo-check] <message>
      argv: codex exec resume [--skip-git-repo-check] <sid> <message>

    Echoes a banner to stderr (\"session: <uuid>\") and the input message
    to stdout. Honors FAKE_EXIT_CODE for failure-path tests.
    \"\"\"
    import os, sys

    args = sys.argv[1:]
    assert args[0] == \"exec\", f\"unexpected first arg: {args[0]!r}\"

    if \"--resume\" in args:
        raise SystemExit(\"--resume is not supported\")

    resume_id = None
    if len(args) > 1 and args[1] == \"resume\":
        i = 2
        while i < len(args):
            if args[i].startswith(\"--\"):
                i += 1
            else:
                break
        resume_id = args[i]
        msg = args[i + 1] if i + 1 < len(args) else \"\"
    else:
        i = 1
        while i < len(args):
            if args[i].startswith(\"--\"):
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
    assert result.exit_code == 0
    assert result.text == "echo: follow up"


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


@pytest.mark.asyncio
async def test_run_uses_exec_resume_subcommand_not_legacy_flag(fake_codex):
    """Codex CLI 0.6+ rejects ``codex exec --resume <sid>``; the correct
    invocation is ``codex exec resume [--skip-git-repo-check] <sid> <prompt>``.
    The fake-codex fixture raises SystemExit if it sees ``--resume`` in
    argv, so the assertion is that the call SUCCEEDS — proving the
    runner moved off the legacy flag.
    """
    runner = CodexRunner(codex_bin=str(fake_codex), timeout_secs=10.0)
    sid = "deadbeef-1111-2222-3333-444455556666"
    result = await runner.run(message="follow", session_id=sid)
    # Subprocess returned 0 because the fake didn't see --resume. If the
    # runner ever regresses to the old shape, the fake exits non-zero
    # with "--resume is not supported" → result.exit_code != 0.
    assert result.exit_code == 0, (
        f"Expected exec resume subcommand shape, got "
        f"exit_code={result.exit_code} stderr_tail={result.stderr_tail!r}"
    )
    assert result.session_id == sid


@pytest.mark.asyncio
async def test_run_prepends_codex_bin_dir_to_subprocess_path(tmp_path):
    """The codex binary is a `#!/usr/bin/env node` shim. Under launchd /
    systemd, the parent process's PATH is stripped to /usr/bin:/bin, so
    the shebang's `env node` lookup fails and codex 127s out silently.
    The runner mitigates this by prepending the codex binary's own
    directory (where node lives in nvm/brew/pnpm-global layouts) to the
    subprocess PATH. Pin: subprocess sees that dir first in PATH.
    """
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    fake = bin_dir / "codex"
    # Echo the resolved subprocess PATH so the test can inspect it.
    fake.write_text(textwrap.dedent("""\
        #!/usr/bin/env python3
        import os, sys
        sys.stderr.write(f"session: a1b2c3d4-1111-2222-3333-444455556666\\n")
        sys.stdout.write(os.environ.get("PATH", ""))
    """))
    fake.chmod(0o755)

    runner = CodexRunner(codex_bin=str(fake), timeout_secs=10.0)
    # Strip PATH down to the launchd-default minimum to prove the
    # runner's own injection puts bin_dir back.
    import os as _os
    saved = _os.environ.get("PATH", "")
    _os.environ["PATH"] = "/usr/bin:/bin"
    try:
        result = await runner.run(message="probe")
    finally:
        _os.environ["PATH"] = saved

    assert result.exit_code == 0
    seen_path = result.text
    # bin_dir must appear FIRST so `env node` finds it before falling
    # back to the (empty for launchd) system PATH.
    assert seen_path.startswith(str(bin_dir) + os.pathsep), (
        f"expected codex_bin_dir prefix in subprocess PATH, got: {seen_path!r}"
    )
    assert "/usr/bin" in seen_path, (
        f"expected existing PATH preserved after prefix, got: {seen_path!r}"
    )


@pytest.mark.asyncio
async def test_run_does_not_duplicate_codex_bin_dir_in_path(tmp_path):
    """If the operator's PATH already includes the codex bin dir (the
    shell-launched case), don't double it — keeps logs and downstream
    tools sane.
    """
    bin_dir = tmp_path / "alreadythere"
    bin_dir.mkdir()
    fake = bin_dir / "codex"
    fake.write_text(textwrap.dedent("""\
        #!/usr/bin/env python3
        import os, sys
        sys.stderr.write(f"session: a1b2c3d4-1111-2222-3333-444455556666\\n")
        sys.stdout.write(os.environ.get("PATH", ""))
    """))
    fake.chmod(0o755)

    runner = CodexRunner(codex_bin=str(fake), timeout_secs=10.0)
    import os as _os
    saved = _os.environ.get("PATH", "")
    _os.environ["PATH"] = f"{bin_dir}:/usr/bin:/bin"
    try:
        result = await runner.run(message="probe")
    finally:
        _os.environ["PATH"] = saved

    seen_path = result.text
    occurrences = seen_path.split(os.pathsep).count(str(bin_dir))
    assert occurrences == 1, (
        f"expected codex_bin_dir to appear exactly once, got {occurrences}: {seen_path!r}"
    )


def test_extract_session_id_returns_none_on_no_match():
    assert _extract_session_id("nothing relevant here") is None
