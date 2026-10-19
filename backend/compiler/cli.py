"""Command line interface.

    python -m compiler workflow.af              # check only, print diagnostics
    python -m compiler workflow.af -o out.py    # also write the generated LangGraph file
"""

import argparse
import os
import sys

from .diagnostics import Diagnostic, format_diagnostic
from .pipeline import compile_source


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="compiler", description="Compile an AgentForge (.af) workflow to LangGraph Python.")
    parser.add_argument("file", help="the .af source file")
    parser.add_argument("-o", "--output", help="where to write the generated Python file")
    args = parser.parse_args(argv)

    with open(args.file, encoding="utf-8") as f:
        source = f.read()
    result = compile_source(source, os.path.basename(args.file))

    for d in result["diagnostics"]:
        diag = Diagnostic(d["severity"], d["code"], d["phase"], d["message"], d["line"], d["col"], d["path"])
        print(format_diagnostic(diag, source, args.file), file=sys.stderr)

    phases = result["phases"]
    print("phases: " + "  ".join(f"{name}={p['status']}" for name, p in phases.items()))
    removed = phases["optimize"]["removed_agents"]
    if removed:
        print("removed dead agents: " + ", ".join(removed))

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
