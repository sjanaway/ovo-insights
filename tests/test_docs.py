"""The documentation can't drift from the code: commands, links, release plumbing."""

import re
import subprocess
from pathlib import Path

import pytest
import typer.main

from ovo_insights.cli import app

ROOT = Path(__file__).resolve().parent.parent
MARKDOWN = [
    ROOT / "README.md",
    ROOT / "CHANGELOG.md",
    ROOT / "CONTRIBUTING.md",
    ROOT / "SECURITY.md",
    *sorted((ROOT / "docs").rglob("*.md")),
]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
NOTES = ROOT / "scripts" / "release-notes.sh"


def rel(path: Path) -> str:
    return str(path.relative_to(ROOT))


# -- the README's commands are real -------------------------------------------------------


def cli_commands() -> set[str]:
    return set(typer.main.get_command(app).commands)  # type: ignore[attr-defined]


def shell_lines(path: Path) -> list[str]:
    text = path.read_text()
    blocks = re.findall(r"```(?:bash|sh)\n(.*?)```", text, flags=re.DOTALL)
    return [line.strip() for block in blocks for line in block.splitlines() if line.strip()]


def test_every_ovo_insights_command_in_the_readme_exists() -> None:
    commands = cli_commands()
    assert {"login", "logout", "doctor", "serve", "demo", "sync"} <= commands
    seen = set()
    for line in shell_lines(ROOT / "README.md"):
        words = line.split()
        if words[:1] == ["ovo-insights"] and len(words) > 1 and not words[1].startswith("-"):
            assert words[1] in commands, line
            seen.add(words[1])
    # Commands named in the prose, like `ovo-insights logout`, are real too.
    inline = re.findall(r"`ovo-insights ([a-z][a-z-]*)[^`]*`", (ROOT / "README.md").read_text())
    for name in inline:
        assert name in commands, name
    assert seen | set(inline) >= {"demo", "login", "doctor", "sync", "serve", "logout"}


def test_the_readme_flags_exist() -> None:
    root = typer.main.get_command(app)
    serve = {opt for p in root.commands["serve"].params for opt in p.opts}  # type: ignore[attr-defined]
    sync = {opt for p in root.commands["sync"].params for opt in p.opts}  # type: ignore[attr-defined]
    demo = {opt for p in root.commands["demo"].params for opt in p.opts}  # type: ignore[attr-defined]
    assert {"--no-open", "--port"} <= serve
    assert "--dry-run" in sync
    assert "--seed" in demo


# -- links and images resolve -------------------------------------------------------------


def github_anchor(heading: str) -> str:
    text = re.sub(r"[`*_]", "", heading).strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def anchors(path: Path) -> set[str]:
    return {
        github_anchor(m)
        for m in re.findall(r"^#{1,6}\s+(.*?)\s*$", path.read_text(), flags=re.MULTILINE)
    }


@pytest.mark.parametrize("path", MARKDOWN, ids=rel)
def test_relative_links_and_images_resolve(path: Path) -> None:
    text = path.read_text()
    targets = re.findall(r"!?\[[^\]]*\]\(([^)\s]+)\)", text)
    for target in targets:
        if re.match(r"[a-z][a-z0-9+.-]*:", target):
            continue  # an external link
        file_part, _, anchor = target.partition("#")
        resolved = path if not file_part else (path.parent / file_part).resolve()
        assert resolved.exists(), f"{rel(path)} links to missing {target}"
        if anchor and resolved.suffix == ".md":
            assert anchor in anchors(resolved), f"{rel(path)} links to missing anchor {target}"


def test_the_readme_has_its_screenshots_and_they_are_small() -> None:
    shots = sorted((ROOT / "docs" / "screenshots").glob("*.jpg"))
    assert len(shots) >= 4
    readme = (ROOT / "README.md").read_text()
    for shot in shots:
        assert f"docs/screenshots/{shot.name}" in readme
        assert shot.stat().st_size < 300_000, shot  # the large-file hook stops at 500 KB


def test_security_doc_cites_only_tests_that_exist() -> None:
    text = (ROOT / "docs" / "security.md").read_text()
    cited = set(re.findall(r"`(tests/[\w/.]+?)`", text))
    assert len(cited) >= 8
    for name in cited:
        assert (ROOT / name).exists(), name


def test_security_md_points_at_the_threat_model() -> None:
    assert "(docs/security.md)" in (ROOT / "SECURITY.md").read_text()


# -- release plumbing ---------------------------------------------------------------------


@pytest.mark.parametrize("path", WORKFLOWS, ids=rel)
def test_every_action_is_pinned_to_a_commit(path: Path) -> None:
    for line in path.read_text().splitlines():
        match = re.match(r"\s*-?\s*uses:\s*(\S+)", line)
        if match and not match.group(1).startswith("./"):
            assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", match.group(1)), line


def test_the_release_workflow_publishes_nowhere_public_and_only_on_tags() -> None:
    text = (ROOT / ".github" / "workflows" / "release.yml").read_text()
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert "pypi" not in code.lower()
    assert "twine" not in code.lower()
    assert "uv publish" not in code
    assert "contents: write" in text
    # Write access exists only in the publish job, which runs only for tags.
    build, _, publish = text.partition("  publish:")
    assert "contents: write" not in build
    assert "github.ref_type == 'tag'" in publish
    assert "--verify-tag" in publish
    assert "SHA256SUMS" in build


def notes(version: str, changelog: Path | None = None) -> subprocess.CompletedProcess[str]:
    args = ["bash", str(NOTES), version]
    if changelog is not None:
        args.append(str(changelog))
    return subprocess.run(args, capture_output=True, text=True, check=False)


def test_release_notes_come_from_the_matching_changelog_section(tmp_path: Path) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        "# Changelog\n\nintro\n\n## [Unreleased]\n\n- soon\n\n"
        "## [1.1.0] - 2026-11-01\n\n### Added\n- two\n- three\n\n\n"
        "## [1.0.0] - 2026-10-20\n\n- one\n\n## [0.9.0]\n"
    )
    assert notes("1.1.0", changelog).stdout == "### Added\n- two\n- three\n"
    assert notes("1.0.0", changelog).stdout == "- one\n"
    assert notes("Unreleased", changelog).stdout == "- soon\n"
    for missing in ("2.0.0", "0.9.0", "1.1"):  # absent, empty, and a prefix of a real one
        result = notes(missing, changelog)
        assert result.returncode == 1, missing
        assert result.stdout == ""
        assert "no (or an empty) section" in result.stderr


def test_the_real_changelog_has_notes_for_the_next_release() -> None:
    result = notes("Unreleased")
    assert result.returncode == 0
    assert result.stdout.strip()
    version = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M)
    assert version is not None
    # Either the current version has its own section, or it is still unreleased.
    assert notes(version.group(1)).returncode in (0, 1)


# -- issue templates ----------------------------------------------------------------------


def test_issue_templates_ask_for_doctor_and_warn_about_secrets() -> None:
    folder = ROOT / ".github" / "ISSUE_TEMPLATE"
    config = (folder / "config.yml").read_text()
    assert "blank_issues_enabled: false" in config
    assert "SECURITY.md" in config
    for name in ("bug_report.yml", "api_change.yml"):
        text = (folder / name).read_text()
        assert "ovo-insights doctor" in text, name
        for word in ("password", "token", "MPAN", "meter serial"):
            assert word in text, (name, word)
        assert "required: true" in text
    assert "api-change" in (folder / "api_change.yml").read_text()
