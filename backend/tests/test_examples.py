"""End to end: every valid example compiles to valid Python, every invalid one gives its expected codes."""

import py_compile
import re

import pytest

from compiler.pipeline import compile_source

from helpers import EXAMPLES_DIR

VALID = sorted((EXAMPLES_DIR / "valid").glob("*.af"))
INVALID = sorted((EXAMPLES_DIR / "invalid").glob("*.af"))


def expected_codes(path):
    """Invalid examples start with a line like `# expect: AF-E101, AF-E102`."""
    match = re.search(r"#\s*expect:\s*(.+)", path.read_text())
    return {code.strip() for code in match.group(1).split(",")}


@pytest.mark.parametrize("path", VALID, ids=lambda p: p.name)
def test_valid_example_compiles(path, tmp_path):
    result = compile_source(path.read_text(), path.name)
    assert result["ok"], result["diagnostics"]
    assert result["diagnostics"] == []
    out = tmp_path / "workflow.py"
    out.write_text(result["phases"]["codegen"]["code"])
    py_compile.compile(str(out), doraise=True)


@pytest.mark.parametrize("path", INVALID, ids=lambda p: p.name)
def test_invalid_example_gives_expected_codes(path):
    result = compile_source(path.read_text(), path.name)
    produced = {d["code"] for d in result["diagnostics"]}
    assert produced == expected_codes(path)


def test_failed_phase_skips_later_phases_but_keeps_earlier_output():
    result = compile_source((EXAMPLES_DIR / "invalid" / "type_mismatch.af").read_text())
    phases = result["phases"]
    assert phases["parser"]["status"] == "ok" and phases["parser"]["ast"] is not None
    assert phases["semantic"]["status"] == "error"
    assert all(phases[p]["status"] == "skipped" for p in ("ir", "verify", "optimize", "codegen"))


def test_verify_failure_still_returns_the_graph():
    result = compile_source((EXAMPLES_DIR / "invalid" / "read_before_write.af").read_text())
    nodes = {n["id"]: n["status"] for n in result["phases"]["ir"]["nodes"]}
    assert nodes["responder"] == "error"
    assert nodes["classifier"] == "ok"
