"""Keep operator and release surfaces aligned with the private registry."""

import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
PRIVATE_SIMPLE = (
    "https://git.moleculesai.app/api/packages/molecule-ai/pypi/simple/"
)


def test_package_uses_current_spdx_license_metadata() -> None:
    with (ROOT / "pyproject.toml").open("rb") as f:
        project = tomllib.load(f)["project"]

    assert project["license"] == "Apache-2.0"


def test_runtime_source_comments_name_the_canonical_distribution() -> None:
    bridge = (ROOT / "codex_channel_molecule/bridge.py").read_text(encoding="utf-8")

    assert "molecule-ai-workspace-runtime" not in bridge
    assert "molecules-workspace-runtime" in bridge


def test_readme_documents_a_clean_dependency_safe_install() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    for required in (
        "download --no-deps",
        PRIVATE_SIMPLE,
        "molecules-workspace-runtime==0.4.4",
        "codex-channel-molecule==0.1.4",
        "pip install --index-url https://pypi.org/simple/",
    ):
        assert required in readme

    for retired in (
        "pip install codex-channel-molecule\n",
        "operator-host SSOT",
        "/etc/molecule-bootstrap/all-credentials.env",
        "PYPI_TOKEN",
    ):
        assert retired not in readme


def test_ci_bootstraps_the_private_runtime_without_an_extra_index() -> None:
    workflow = (ROOT / ".gitea/workflows/ci.yml").read_text(encoding="utf-8")

    assert "pip download --no-deps" in workflow
    assert PRIVATE_SIMPLE in workflow
    assert "molecules-workspace-runtime==0.4.4" in workflow
    assert "pip install --no-deps -e ." in workflow
    assert "--extra-index-url" not in workflow


def test_publish_targets_gitea_with_the_infisical_package_identity() -> None:
    workflow = (ROOT / ".gitea/workflows/publish.yml").read_text(encoding="utf-8")

    for required in (
        "GITEA_PYPI_UPLOAD_URL",
        "MOL_PACKAGE_TOKEN",
        "%2Fci%2Fgitea-pypi-publisher",
        "--repository-url",
        "download --no-deps",
        'RUNTIME_VERSION: "0.4.4"',
        "molecules-workspace-runtime==$RUNTIME_VERSION",
    ):
        assert required in workflow

    for retired in ("PYPI_TOKEN", "/ci/pypi", "--repository pypi"):
        assert retired not in workflow
