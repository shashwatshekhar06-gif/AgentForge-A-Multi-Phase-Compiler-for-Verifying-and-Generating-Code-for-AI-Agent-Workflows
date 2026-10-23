"""FastAPI server for the web UI.

Run from the backend folder:  uvicorn api:app --reload --port 8000

/api/compile never calls a model: compare blocks are scored with the mock answers written in the .af file.
/api/compile-real is the one endpoint that makes paid API calls. It exists only while a key is available to the
server (OPENAI_API_KEY or ANTHROPIC_API_KEY in the environment or in backend/.env), is limited to MAX_REAL_CALLS
model calls and one run at a time, and can be switched off with AGENTFORGE_DISABLE_REAL=1. The key is read by the
server only; it is never sent to the browser or put in a response.
"""

import os
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from compiler.cli import measure_with_real_model
from compiler.envfile import load_env
from compiler.pipeline import compile_source

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"

MAX_REAL_CALLS = 60          # upper bound on model calls for one measurement
REAL_TIMEOUT_SECONDS = 180   # the profiling run is stopped after this long

app = FastAPI(title="AgentForge")
real_run = threading.Lock()   # one measurement at a time


class CompileRequest(BaseModel):
    source: str


def real_profile_available() -> bool:
    if os.environ.get("AGENTFORGE_DISABLE_REAL") == "1":
        return False
    load_env()
    return bool(os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY"))


@app.post("/api/compile")
def compile_endpoint(request: CompileRequest) -> dict:
    return compile_source(request.source)


@app.get("/api/real-profile")
def real_profile_status() -> dict:
    """Is measuring with a real model possible? (A yes/no and a limit; never the key.)"""
    return {"available": real_profile_available(), "max_calls": MAX_REAL_CALLS}


@app.post("/api/compile-real")
def compile_real_endpoint(request: CompileRequest) -> dict:
    """Score the compare blocks with a real model, then compile again using the measured scores."""
    if not real_profile_available():
        raise HTTPException(403, "no API key is available to the server (or measuring with a real model is switched off)")
    first = compile_source(request.source, select=False)
    if not first["ok"]:
        raise HTTPException(400, "fix the errors in the program first; there is nothing to measure yet")
    calls = sum(r.get("model_calls", 0) for r in first["evaluation"] + first["necessity"])
    if not first["evaluation"] and not first["necessity"]:
        raise HTTPException(400, "this program has no compare or necessity block, so there is nothing to measure")
    if calls > MAX_REAL_CALLS:
        raise HTTPException(400, f"this would make up to {calls} model calls; the limit is {MAX_REAL_CALLS}. "
                                 f"Use fewer test cases or candidates, or run it from the command line")
    if not real_run.acquire(blocking=False):
        raise HTTPException(409, "another measurement is still running")
    try:
        profile, problem = measure_with_real_model(request.source, "workflow.af", timeout=REAL_TIMEOUT_SECONDS)
    finally:
        real_run.release()
    if problem:
        raise HTTPException(502, f"profiling failed: {problem}")
    return compile_source(request.source, profile=profile)


@app.get("/api/examples")
def list_examples() -> list:
    examples = []
    for category in ("valid", "invalid"):
        for path in sorted((EXAMPLES_DIR / category).glob("*.af")):
            examples.append({"name": path.name, "category": category,
                             "source": path.read_text(encoding="utf-8")})
    return examples
