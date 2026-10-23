"""Command line interface.

    python -m compiler workflow.af                    # check only, print diagnostics
    python -m compiler workflow.af -o out.py          # also write the generated LangGraph file
    python -m compiler workflow.af -o out.py --profile real
                                                      # measure the compare candidates with a real model first,
                                                      # then generate code that uses the best one
    python -m compiler workflow.af --no-select        # keep the agents wired in the flow, whatever the scores say
                                                      # (also: do not remove agents a necessity test found unneeded)
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile

from .diagnostics import Diagnostic, format_diagnostic
from .envfile import load_env
from .pipeline import compile_source

SCORES_MARKER = "AGENTFORGE_SCORES="
NECESSITY_MARKER = "AGENTFORGE_NECESSITY="


def run_harness(code: str, flag: str, marker: str, timeout: int):
    """Run a generated file's test harness (`flag`) against the real model and read back the JSON it prints."""
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "profile_run.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write(code)
        env = {**os.environ, "AGENTFORGE_LLM": "real"}
        try:
            done = subprocess.run([sys.executable, path, flag], capture_output=True, text=True, env=env, timeout=timeout)
        except subprocess.TimeoutExpired:
            return None, f"the profiling run took longer than {timeout} seconds and was stopped"
    if done.returncode != 0:
        lines = [line for line in done.stderr.strip().splitlines() if line.strip()]
        return None, lines[-1] if lines else f"the profiling run failed (exit code {done.returncode})"
    for line in done.stdout.splitlines():
        if line.startswith(marker):
            return json.loads(line[len(marker):]), None
    return None, "the profiling run printed no scores"


def measure_with_real_model(source: str, name: str, timeout: int = 600):
    """Profile-guided step: run the generated file's test harness against a real model and return its scores.
    This is the only place the compiler's tooling makes API calls, and only when asked (--profile real).
    Returns ({"compare": [...], "necessity": [...]}, None), or (None, why it failed)."""
    first = compile_source(source, name, select=False)
    if not first["ok"]:
        return None, "the program has errors, so there is nothing to measure"
    profile = {"compare": [], "necessity": []}
    if first["evaluation"]:
        scores, problem = run_harness(first["phases"]["codegen"]["code"], "--scores-json", SCORES_MARKER, timeout)
        if problem:
            return None, problem
        profile["compare"] = scores
    if first["necessity"]:
        # test which agents are needed in the workflow that will really be generated: after the winners are chosen
        chosen = compile_source(source, name, profile=profile["compare"] or None, select=True)
        measured, problem = run_harness(chosen["phases"]["codegen"]["code"], "--necessity-json", NECESSITY_MARKER, timeout)
        if problem:
            return None, problem
        profile["necessity"] = measured
    return profile, None


def print_necessity(report: dict):
    if report["mode"] == "static":
        print(f"necessity {report['name']} (not measured: use --profile real to test which agents are needed)")
    else:
        print(f"necessity {report['name']} (real model): the workflow as written passes {report['baseline']}/{report['total']} cases")
    for row in report["agents"]:
        if row["status"] != "testable":
            verdict = f"{row['status']}: {row['reason']}"
        elif row["needed"] is None:
            verdict = "can be tested"
        elif row["score_without"] is None:
            verdict = "not tested (the workflow as written fails every case)"
        elif row["needed"]:
            verdict = f"needed ({row['score_without']}/{report['total']} without it)"
        else:
            verdict = f"not needed ({row['score_without']}/{report['total']} without it)" + (
                f"; removed from the generated code, saving {row['llm_calls']} LLM call(s) per run" if row["removed"] else "")
        print(f"  {row['agent']}: {verdict}")
    for why in report["blocked"]:
        print(f"  not applied: {why}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="compiler", description="Compile an AgentForge (.af) workflow to LangGraph Python.")
    parser.add_argument("file", help="the .af source file")
    parser.add_argument("-o", "--output", help="where to write the generated Python file")
    parser.add_argument("--profile", choices=["mock", "real"], default="mock",
                        help="where the compare scores come from: the answers scripted in the file (mock, default) "
                             "or a real model (real; needs OPENAI_API_KEY or ANTHROPIC_API_KEY, makes paid API calls)")
    parser.add_argument("--no-select", action="store_true",
                        help="do not apply what was measured: keep the agents wired in the flow and remove none")
    args = parser.parse_args(argv)

    with open(args.file, encoding="utf-8") as f:
        source = f.read()
    name = os.path.basename(args.file)
    loaded = load_env()
    if loaded and args.profile == "real":
        print("read from .env: " + ", ".join(loaded))   # names only, never the values

    profile = None
    if args.profile == "real":
        print("measuring with a real model (this makes API calls)...")
        profile, problem = measure_with_real_model(source, name)
        if problem:
            print(f"profiling failed: {problem}", file=sys.stderr)
            return 2
    result = compile_source(source, name, profile=profile, select=not args.no_select)

    for d in result["diagnostics"]:
        diag = Diagnostic(d["severity"], d["code"], d["phase"], d["message"], d["line"], d["col"], d["path"])
        print(format_diagnostic(diag, source, args.file), file=sys.stderr)

    phases = result["phases"]
    print("phases: " + "  ".join(f"{name}={p['status']}" for name, p in phases.items()))
    removed = phases["optimize"]["removed_agents"]
    if removed:
        print("removed dead agents: " + ", ".join(removed))

    for report in result.get("evaluation", []):
        scores = ", ".join(f"{s['agent']} {s['passed']}/{s['total']}" for s in report["scores"])
        source_of_scores = "real model" if report["mode"] == "measured" else "mock answers"
        print(f"compare {report['name']} ({source_of_scores}): {scores}; best: {report['best']}")
        if report["applied"]:
            print(f"  optimizer: '{report['wired'][0]}' replaced by '{report['applied']}' in the generated code")
        elif report["blocked"]:
            print(f"  not applied: {report['blocked']}")
        elif args.no_select:
            for slot in report["wired"]:
                if slot != report["best"]:
                    print(f"  '{slot}' is in the flow but '{report['best']}' scored higher (not applied: --no-select)")

    for report in result.get("necessity", []):
        print_necessity(report)

    if not result["ok"]:
        print("compilation failed", file=sys.stderr)
        return 1
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(phases["codegen"]["code"])
        print(f"wrote {args.output}")
    else:
        print("ok (use -o out.py to write the generated code)")
    return 0
