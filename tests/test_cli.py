import runpy
import subprocess
import sys

import pytest
from typer.testing import CliRunner

from ovo_insights import __version__
from ovo_insights.cli import _print_version, app

runner = CliRunner()


def test_version_flag_prints_version() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout == f"ovo-insights {__version__}\n"


def test_no_arguments_shows_help() -> None:
    result = runner.invoke(app, [])

    assert "Usage" in result.output


def test_version_is_not_placeholder() -> None:
    assert __version__
    assert __version__ != "0.0.0"


def test_installed_entry_point_runs() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "ovo_insights", "--version"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"ovo-insights {__version__}"


def test_version_callback_is_a_no_op_when_flag_absent() -> None:
    _print_version(False)


def test_module_entry_point_runs_the_cli() -> None:
    with pytest.raises(SystemExit) as exc_info:
        runpy.run_module("ovo_insights", run_name="__main__", alter_sys=True)

    # No arguments: Typer shows help and exits with a usage status.
    assert exc_info.value.code == 2
