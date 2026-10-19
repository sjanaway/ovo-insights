"""Guards the runtime dependency surface agreed in the PRD (M1).

Adding a runtime dependency means adding it here, in the same PR, with a one-line reason.
"""

import re
import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"

ALLOWED_RUNTIME_DEPENDENCIES = {
    "fastapi": "web framework (M6)",
    "uvicorn": "ASGI server bound to 127.0.0.1 (M6)",
    "jinja2": "server-rendered, autoescaped templates (M6)",
    "httpx": "async HTTP client with explicit timeouts (M3)",
    "pydantic": "validation of OVO responses and our own models (M2/M3)",
    "keyring": "OS keychain for the OVO password (M4)",
    "platformdirs": "per-OS data directory (M2)",
    "typer": "command-line interface (M1)",
}

_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _normalise(requirement: str) -> str:
    match = _NAME.match(requirement)
    assert match, f"Unparseable requirement: {requirement!r}"
    return re.sub(r"[-_.]+", "-", match.group(1)).lower()


def _project() -> dict[str, object]:
    with PYPROJECT.open("rb") as f:
        project = tomllib.load(f)["project"]
    assert isinstance(project, dict)
    return project


def test_runtime_dependencies_are_on_the_allowlist() -> None:
    dependencies = _project()["dependencies"]
    assert isinstance(dependencies, list)

    unexpected = {_normalise(d) for d in dependencies} - ALLOWED_RUNTIME_DEPENDENCIES.keys()

    assert not unexpected, (
        f"Runtime dependencies not on the allowlist: {sorted(unexpected)}. "
        "Add them to ALLOWED_RUNTIME_DEPENDENCIES with a reason, or make them dev-only."
    )


def test_no_optional_dependency_extras_sneak_in_runtime_packages() -> None:
    assert "optional-dependencies" not in _project()


def test_package_cannot_be_uploaded_to_pypi() -> None:
    # PyPI publishing isn't set up; this classifier makes PyPI reject any accidental upload.
    classifiers = _project()["classifiers"]
    assert isinstance(classifiers, list)
    assert "Private :: Do Not Upload" in classifiers


def test_normalise_handles_markers_and_case() -> None:
    assert _normalise("Typer[all]>=0.27; python_version>='3.13'") == "typer"
    assert _normalise("ruamel.yaml==0.18") == "ruamel-yaml"
