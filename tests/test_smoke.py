"""Smoke tests for the starter project."""

from agent_code_reviewer import __version__
from agent_code_reviewer.cli import main


def test_package_version() -> None:
    assert __version__ == "0.1.0"


def test_cli_returns_zero() -> None:
    assert main([]) == 0
