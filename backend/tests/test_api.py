from fastapi.testclient import TestClient

from api import app

client = TestClient(app)


def test_compile_endpoint_shape():
    source = 'state S { input q: string\n output a: string }\nagent x { state.a = llm("hi", state.q) }\nflow f { start -> x -> end }'
    data = client.post("/api/compile", json={"source": source}).json()
    assert data["ok"] is True
    assert list(data["phases"]) == ["lexer", "parser", "semantic", "ir", "verify", "optimize", "cost", "codegen"]
    assert data["phases"]["lexer"]["tokens"][0] == {"type": "STATE", "lexeme": "state", "line": 1, "col": 1}
    edge = data["phases"]["ir"]["edges"][0]
    assert set(edge) == {"from", "to", "condition", "max_iterations", "is_default"}


def test_compile_endpoint_reports_errors():
    data = client.post("/api/compile", json={"source": "agent @"}).json()
    assert data["ok"] is False
    assert data["diagnostics"][0]["code"] == "AF-E001"
    assert data["phases"]["parser"]["status"] == "skipped"


def test_examples_endpoint():
    examples = client.get("/api/examples").json()
    names = {e["name"] for e in examples}
    assert {"research.af", "fraud.af", "read_before_write.af"} <= names
