"""Pin the canonical runtime distribution and its minimum version.

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

The runtime distribution was renamed from the retired
``molecule-ai-workspace-runtime`` name to ``molecules-workspace-runtime`` at
0.4.0. The bridge must use that canonical name: package managers treat the two
names as distinct projects, so keeping the retired dependency makes a clean
install fail even when the canonical wheel is already present.

This test also holds the current supported floor at ``>=0.4.4``. That release
contains the SSOT response parser described above plus the current standalone
runtime contract consumed by this bridge.
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


_CANONICAL_RUNTIME_DISTRIBUTION = "molecules-workspace-runtime"
_MINIMUM_RUNTIME_VERSION = (0, 4, 4)


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
    """Return the raw dependency specifier for the canonical runtime."""
    root = Path(__file__).resolve().parent.parent
    pyproject = root / "pyproject.toml"
    with pyproject.open("rb") as f:
        data = tomllib.load(f)
    deps = data["project"]["dependencies"]
    for dep in deps:
        name = re.split(r"[<>=!~]", dep, maxsplit=1)[0]
        normalized = re.sub(r"[-_.]+", "-", name).lower()
        if normalized == _CANONICAL_RUNTIME_DISTRIBUTION:
            return dep
    raise AssertionError(
        "pyproject.toml is missing the canonical "
        f"{_CANONICAL_RUNTIME_DISTRIBUTION} dependency"
    )


def test_retired_runtime_distribution_is_not_a_dependency() -> None:
    """The pre-0.4 package name is a different, retired distribution."""
    root = Path(__file__).resolve().parent.parent
    with (root / "pyproject.toml").open("rb") as f:
        deps = tomllib.load(f)["project"]["dependencies"]

    normalized_names = {
        re.sub(r"[-_.]+", "-", re.split(r"[<>=!~]", dep, maxsplit=1)[0]).lower()
        for dep in deps
    }
    assert "molecule-ai-workspace-runtime" not in normalized_names


def test_runtime_floor_includes_a2a_response_parser(runtime_specifier: str) -> None:
    """The runtime floor must be at or above the SSOT parser release."""
    bound = _parse_version(runtime_specifier)
    assert bound >= _MINIMUM_RUNTIME_VERSION, (
        f"runtime floor {bound} is below {_MINIMUM_RUNTIME_VERSION} — "
        f"the supported canonical runtime contract starts at 0.4.4; older "
        f"or retired distributions can misclassify the poll-mode "
        f"queued envelope as malformed and surfaces "
        f"'[A2A_ERROR] unexpected response shape' on every poll-mode peer "
        f"reply. See molecule-ai/internal#424."
    )


def test_runtime_specifier_uses_a_lower_bound(runtime_specifier: str) -> None:
    """A bare ``pkg`` or upper-only spec would silently install ANY version
    on a fresh ``pip install`` — including unsupported pre-0.4 releases.

    Require an explicit lower bound (``>=`` or ``~=``).
    """
    assert re.search(r">=|~=", runtime_specifier), (
        f"runtime dependency {runtime_specifier!r} has no lower bound — "
        f"a fresh install could resolve to an unsupported wheel with the "
        f"broken poll-mode parser"
    )
