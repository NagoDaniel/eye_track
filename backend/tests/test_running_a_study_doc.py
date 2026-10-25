"""`docs/RUNNING_A_STUDY.md` names only commands, options, scripts, files that exist (PLAN §13)."""

import re
from pathlib import Path

import pytest
import typer.main

import cli

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "RUNNING_A_STUDY.md"
TEXT = DOC.read_text(encoding="utf-8")
COMMANDS = typer.main.get_command(cli.app).commands  # type: ignore[attr-defined]


def cli_calls() -> list[tuple[str, list[str]]]:
    """Every `cli.py <command> ...` inside backticks, with the options written after it."""
    calls = []
    for code in re.findall(r"`([^`\n]*cli\.py [^`\n]*)`", TEXT):
        for part in re.split(r"`|, (?=cli\.py)", code):
            match = re.search(r"cli\.py (\S+)(.*)", part)
            if match and not match.group(1).startswith("<"):  # `cli.py <command>` is the pattern
                calls.append((match.group(1), re.findall(r"--[a-z][a-z-]*", match.group(2))))
    return calls


def test_the_guide_has_commands_to_check() -> None:
    assert len(cli_calls()) >= 10  # the table of section 8 and the ones in the text


@pytest.mark.parametrize(("command", "options"), cli_calls())
def test_every_command_and_option_in_the_guide_exists(command: str, options: list[str]) -> None:
    assert command in COMMANDS, f"cli.py has no command '{command}'"
    known = {opt for param in COMMANDS[command].params for opt in param.opts}
    assert set(options) <= known, (
        f"{command}: {sorted(set(options) - known)} not in {sorted(known)}"
    )


def test_the_scripts_and_files_it_names_exist() -> None:
    for script in set(re.findall(r"\./(scripts/[\w.-]+\.sh)", TEXT)):
        assert (ROOT / script).is_file(), script
    for path in set(re.findall(r"`((?:e2e|backend|docs|shared)/[\w./<>-]+)`", TEXT)):
        if "<" in path:
            continue  # a pattern such as backend/app/consent/<version>.txt
        assert (ROOT / path).exists(), path
    for link in set(re.findall(r"\]\(([^)#]+?)(?:#[^)]*)?\)", TEXT)):
        if not link.startswith("http"):
            assert (DOC.parent / link).resolve().exists(), link


def test_the_numbers_it_quotes_are_the_config_defaults() -> None:
    from worker.pipeline.config import PipelineConfig

    config = PipelineConfig()
    assert f"{config.dot_max_median_error_norm} of the screen diagonal" in TEXT
    assert f"{config.dot_min_valid_fraction:.0%}".replace("%", " %") in TEXT
    assert f"{config.dropped_frames_flag_fraction:.0%}".replace("%", " %") in TEXT
