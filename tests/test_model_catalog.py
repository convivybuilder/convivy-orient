"""`model_catalog.load` — the dispatchable models, from `agentctl models --json`
(issue_01M3NWDHCDQK6PE9NEXFQ37G9K).

Each test runs a real executable standing in for agentctl, so the subprocess
call, its exit code and its stdout are all exercised rather than stubbed.
"""
from __future__ import annotations

import json
import stat

import pytest

import model_catalog
import paths

_GOOD = {"schema_version": 1, "models": [
    {"alias": "opus", "model": "claude-opus-5-5", "provider": "anthropic"},
    {"alias": "gpt5", "model": "openai/gpt-5", "provider": "gateway"},
    {"alias": None, "model": "claude-opus-4-8", "provider": "anthropic"},
]}


@pytest.fixture
def agentctl(tmp_path, monkeypatch):
    """Install a fake agentctl that prints `stdout` and exits `code`, and
    records its argv so a test can see what was asked."""
    argv_file = tmp_path / "argv"

    def install(stdout: str, code: int = 0):
        out = tmp_path / "stdout"
        out.write_text(stdout)
        exe = tmp_path / "agentctl"
        exe.write_text(f'#!/bin/sh\necho "$@" > {argv_file}\ncat {out}\nexit {code}\n')
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
        monkeypatch.setattr(paths, "resolve_tool", lambda name: str(exe))
        return argv_file

    return install


def test_the_list_comes_back_in_agentctls_order(agentctl):
    argv = agentctl(json.dumps(_GOOD))
    got = model_catalog.load()
    assert [m.model for m in got] == ["claude-opus-5-5", "openai/gpt-5", "claude-opus-4-8"]
    assert got[0].alias == "opus" and got[2].alias is None
    assert argv.read_text().split() == ["models", "--json"]


def test_a_nonzero_exit_is_unavailable_even_with_good_stdout(agentctl):
    agentctl(json.dumps(_GOOD), code=1)
    assert model_catalog.load() is None


@pytest.mark.parametrize("stdout", ["not json", "", "[1, 2]", '{"schema_version": 1}'])
def test_unparseable_stdout_is_unavailable(agentctl, stdout):
    agentctl(stdout)
    assert model_catalog.load() is None


@pytest.mark.parametrize("version", [2, 0, "1", None])
def test_an_unknown_schema_version_is_unavailable(agentctl, version):
    agentctl(json.dumps({**_GOOD, "schema_version": version}))
    assert model_catalog.load() is None


@pytest.mark.parametrize("row", [
    {"alias": "x", "provider": "anthropic"},
    {"alias": "x", "model": "", "provider": "anthropic"},
    {"alias": 5, "model": "m", "provider": "anthropic"},
    "claude-opus-5-5",
])
def test_a_malformed_row_makes_the_whole_list_unavailable(agentctl, row):
    """A partial list would offer a subset as though it were the whole."""
    agentctl(json.dumps({"schema_version": 1, "models": [*_GOOD["models"], row]}))
    assert model_catalog.load() is None


def test_no_agentctl_is_unavailable(monkeypatch):
    monkeypatch.setattr(paths, "resolve_tool", lambda name: None)
    assert model_catalog.load() is None


def test_only_home_and_path_reach_agentctl(tmp_path, monkeypatch):
    env_file = tmp_path / "env"
    exe = tmp_path / "agentctl"
    exe.write_text(f"#!/bin/sh\nenv > {env_file}\necho '{json.dumps(_GOOD)}'\n")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(paths, "resolve_tool", lambda name: str(exe))
    monkeypatch.setenv("ORIENT_SECRET_FOR_TEST", "leak")
    assert model_catalog.load() is not None
    names = {line.split("=", 1)[0] for line in env_file.read_text().splitlines()}
    assert "ORIENT_SECRET_FOR_TEST" not in names
    assert "PATH" in names
