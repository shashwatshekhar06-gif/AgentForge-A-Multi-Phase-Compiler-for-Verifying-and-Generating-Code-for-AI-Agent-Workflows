"""Reading API keys from a .env file: the compiler's loader and the one inside generated files."""

import os
import subprocess
import sys

import pytest

from compiler import envfile
from compiler.pipeline import compile_source

from helpers import EXAMPLES_DIR, TESTS_DIR


def test_parse_env_handles_comments_quotes_and_blank_values():
    text = """
# a comment
OPENAI_API_KEY=sk-abc
ANTHROPIC_API_KEY = "quoted value"
AGENTFORGE_MODEL='gpt-4.1'
EMPTY=
no equals sign here
  SPACED  =  padded
KEY_WITH_EQUALS=a=b=c
"""
    assert envfile.parse_env(text) == {
        "OPENAI_API_KEY": "sk-abc", "ANTHROPIC_API_KEY": "quoted value", "AGENTFORGE_MODEL": "gpt-4.1",
        "SPACED": "padded", "KEY_WITH_EQUALS": "a=b=c"}


def test_load_env_sets_missing_variables_only_and_returns_names(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTFORGE_NO_DOTENV")
    monkeypatch.delenv("AF_TEST_NEW", raising=False)
    monkeypatch.setenv("AF_TEST_EXISTING", "from the terminal")
    path = tmp_path / ".env"
    path.write_text("AF_TEST_NEW=from the file\nAF_TEST_EXISTING=from the file\n")
    loaded = envfile.load_env([path])
    assert loaded == ["AF_TEST_NEW"]                                  # names only, never values
    assert os.environ["AF_TEST_NEW"] == "from the file"
    assert os.environ["AF_TEST_EXISTING"] == "from the terminal"      # the real environment wins
    monkeypatch.delenv("AF_TEST_NEW")


def test_load_env_ignores_a_missing_file(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTFORGE_NO_DOTENV")
    assert envfile.load_env([tmp_path / "nope.env"]) == []


def test_load_env_can_be_switched_off(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTFORGE_NO_DOTENV", "1")
    monkeypatch.delenv("AF_TEST_OFF", raising=False)
    path = tmp_path / ".env"
    path.write_text("AF_TEST_OFF=1\n")
    assert envfile.load_env([path]) == [] and "AF_TEST_OFF" not in os.environ


def test_cli_names_the_variables_it_read_but_never_prints_a_value(tmp_path, monkeypatch, capsys):
    from compiler import cli

    monkeypatch.delenv("AGENTFORGE_NO_DOTENV")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-very-secret-value\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "measure_with_real_model", lambda source, name: ([], None))
    source = tmp_path / "research.af"
    source.write_text((EXAMPLES_DIR / "valid" / "research.af").read_text())
    assert cli.main([str(source), "--profile", "real"]) == 0
    printed = capsys.readouterr()
    assert "read from .env: OPENAI_API_KEY" in printed.out
    assert "sk-very-secret-value" not in printed.out + printed.err
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


@pytest.mark.parametrize("real_env_key, expected", [(None, "sk-from-file"), ("sk-from-terminal", "sk-from-terminal")])
def test_generated_file_reads_dotenv_from_its_folder(tmp_path, real_env_key, expected):
    code = compile_source((EXAMPLES_DIR / "valid" / "research.af").read_text())["phases"]["codegen"]["code"]
    (tmp_path / "workflow.py").write_text(code)
    (tmp_path / ".env").write_text('OPENAI_API_KEY="sk-from-file"\n')
    env = {k: v for k, v in os.environ.items() if k not in ("AGENTFORGE_NO_DOTENV", "OPENAI_API_KEY")}
    if real_env_key:
        env["OPENAI_API_KEY"] = real_env_key
    probe = "import workflow, os; print(os.environ.get('OPENAI_API_KEY'))"
    out = subprocess.run([sys.executable, "-c", probe], cwd=tmp_path, capture_output=True, text=True, env=env, timeout=120)
    if "No module named 'langgraph'" in out.stderr:
        pytest.skip("langgraph is not installed")
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == expected


def test_dotenv_is_git_ignored_and_an_example_is_provided():
    root = TESTS_DIR.parent.parent
    ignored = (root / ".gitignore").read_text().splitlines()
    assert ".env" in ignored and "!.env.example" in ignored
    example = (root / ".env.example").read_text()
    assert "OPENAI_API_KEY=" in example and "sk-" not in example     # a placeholder, never a real key
