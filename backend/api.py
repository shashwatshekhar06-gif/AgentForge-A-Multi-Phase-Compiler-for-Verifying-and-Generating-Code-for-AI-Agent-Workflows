"""FastAPI server for the web UI.

Run from the backend folder:  uvicorn api:app --reload --port 8000
"""

from pathlib import Path

from fastapi import FastAPI
from pydantic import BaseModel

from compiler.pipeline import compile_source

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"

app = FastAPI(title="AgentForge")


class CompileRequest(BaseModel):
    source: str


@app.post("/api/compile")
def compile_endpoint(request: CompileRequest) -> dict:
    return compile_source(request.source)


@app.get("/api/examples")
def list_examples() -> list:
    examples = []
    for category in ("valid", "invalid"):
        for path in sorted((EXAMPLES_DIR / category).glob("*.af")):
            examples.append({"name": path.name, "category": category,
                             "source": path.read_text(encoding="utf-8")})
    return examples
