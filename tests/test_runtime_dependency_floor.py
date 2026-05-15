"""Pin the minimum molecule-ai-workspace-runtime version that ships
the SSOT A2A response parser.

Background — see ``molecule-ai/internal#424``:

Pre-runtime-0.1.129 the inline sniffer in ``a2a_client.send_a2a_message``
checked for ``result`` or ``error`` keys and routed everything else to
``[A2A_ERROR] unexpected response shape (no result, no error): ...``.
That branch fired on every reply attempt to a poll-mode peer because the
platform proxy synthesizes the success envelope as
``{"status": "queued", "delivery_mode": "poll", "method": "..."}``
which has NEITHER ``result`` NOR ``error``. The retry loop hammered the
caller's canvas with the error string every ~3s.

Version 0.1.129 introduced ``a2a_response.py`` — a typed parser with an
explicit ``Queued`` variant — and the matching ``[A2A_QUEUED]`` sentinel
in ``a2a_client.py``. ``a2a_tools_delegation.tool_delegate_task`` then
falls back to the durable ``/delegate`` + ``/delegations`` polling path,
which IS the correct synchronous facade for poll-mode peers.

This test asserts the floor is held at ``>=0.1.129`` so a future
dependency-housekeeping pass cannot silently lower it back into the
broken range. If the floor is ever raised further (e.g. to require a
later SSOT parser feature), update the constant below — the lower bound
must never go below 0.1.129.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - Python 3.11+ required by pyproject anyway
    import tomli as tomllib


# The earliest runtime version that ships the SSOT A2A response parser
# with the typed ``Queued`` variant. Bumping this floor MUST be paired
# with a comment update in pyproject.toml's dependencies block.
_MINIMUM_RUNTIME_VERSION = (0, 1, 129)


def _parse_version(spec: str) -> tuple[int, int, int]:
    """Extract the lower-bound version from a dependency specifier.

    Supports the common shapes used in our pyproject files:

      * ``pkg>=1.2.3``         → (1, 2, 3)
      * ``pkg>=1.2.3,<2``      → (1, 2, 3)
      * ``pkg~=1.2.3``         → (1, 2, 3)

    Raises ``ValueError`` on anything else so the test fails loud rather
    than silently passing on a malformed specifier.
    """
    m = re.search(r"(?:>=|~=)\s*(\d+)\.(\d+)\.(\d+)", spec)
    if not m:
        raise ValueError(f"no lower-bound version in specifier: {spec!r}")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


@pytest.fixture(scope="module")
def runtime_specifier() -> str:
    """Return the raw dependency specifier for molecule-ai-workspace-runtime."""
    root = Path(__file__).resolve().parent.parent
    pyproject = root / "pyproject.toml"
    with pyproject.open("rb") as f:
        data = tomllib.load(f)
    deps = data["project"]["dependencies"]
    for dep in deps:
        # match the package name allowing a hyphen-or-underscore in case
        # the spec ever normalizes — PEP 503 treats them equivalently.
        if re.match(r"^molecule[-_]ai[-_]workspace[-_]runtime\b", dep):
            return dep
    raise AssertionError(
        "pyproject.toml is missing a molecule-ai-workspace-runtime dependency"
    )


def test_runtime_floor_includes_a2a_response_parser(runtime_specifier: str) -> None:
    """The runtime floor must be at or above the SSOT parser release."""
    bound = _parse_version(runtime_specifier)
    assert bound >= _MINIMUM_RUNTIME_VERSION, (
        f"runtime floor {bound} is below {_MINIMUM_RUNTIME_VERSION} — "
        f"pre-0.1.129 the A2A response parser misclassifies the poll-mode "
        f"queued envelope as malformed and surfaces "
        f"'[A2A_ERROR] unexpected response shape' on every poll-mode peer "
        f"reply. See molecule-ai/internal#424."
    )


def test_runtime_specifier_uses_a_lower_bound(runtime_specifier: str) -> None:
    """A bare ``pkg`` or upper-only spec would silently install ANY version
    on a fresh ``pip install`` — including the buggy pre-0.1.129 range.

    Require an explicit lower bound (``>=`` or ``~=``).
    """
    assert re.search(r">=|~=", runtime_specifier), (
        f"runtime dependency {runtime_specifier!r} has no lower bound — "
        f"a fresh install could resolve to a pre-0.1.129 wheel with the "
        f"broken poll-mode parser"
    )
