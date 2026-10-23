"""The web API's one endpoint that can make paid calls: /api/compile-real. The measurement itself is faked here."""

import json

import pytest
from fastapi.testclient import TestClient

import api
from helpers import EXAMPLES_DIR

EXAMPLE = (EXAMPLES_DIR / "valid" / "compare_agents.af").read_text()


@pytest.fixture
def client(monkeypatch):
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "AGENTFORGE_DISABLE_REAL"):
        monkeypatch.delenv(name, raising=False)
    return TestClient(api.app)


def measured(a, b):
    """The report a generated file prints with --scores-json, for compare_agents.af."""
    def case(question, answer, outcome):
        return {"inputs": {"query": question}, "expect": {"field": "response", "op": "contains", "value": answer},
                "results": [{"agent": "responder_a", "output": "x", "passed": outcome[0]},
                            {"agent": "responder_b", "output": "y", "passed": outcome[1]}]}
    flags = list(zip(a, b))
    return [{"name": "best_responder", "candidates": ["responder_a", "responder_b"],
             "cases": [case("France?", "Paris", flags[0]), case("Japan?", "Tokyo", flags[1]), case("Spain?", "Madrid", flags[2])]}]


def test_status_says_whether_a_key_exists_and_never_shows_it(client, monkeypatch):
    assert client.get("/api/real-profile").json() == {"available": False, "max_calls": api.MAX_REAL_CALLS}
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-value")
    response = client.get("/api/real-profile")
    assert response.json() == {"available": True, "max_calls": api.MAX_REAL_CALLS}
    assert "sk-secret-value" not in response.text


def test_it_can_be_switched_off(client, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("AGENTFORGE_DISABLE_REAL", "1")
    assert client.get("/api/real-profile").json()["available"] is False
    assert client.post("/api/compile-real", json={"source": EXAMPLE}).status_code == 403


def test_without_a_key_nothing_is_measured(client, monkeypatch):
    monkeypatch.setattr(api, "measure_with_real_model", lambda *a, **k: pytest.fail("must not measure without a key"))
    assert client.post("/api/compile-real", json={"source": EXAMPLE}).status_code == 403


def test_it_recompiles_with_the_measured_scores(client, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    seen = {}

    def fake_measure(source, name, timeout=None):
        seen["timeout"] = timeout
        return measured([True, True, True], [False, False, True]), None

    monkeypatch.setattr(api, "measure_with_real_model", fake_measure)
    body = client.post("/api/compile-real", json={"source": EXAMPLE}).json()
    report = body["evaluation"][0]
    assert report["mode"] == "measured" and report["best"] == "responder_a"
    assert report["applied"] is None                                    # the measurement overruled the scripted answers
    assert seen["timeout"] == api.REAL_TIMEOUT_SECONDS
    assert "sk-test" not in json.dumps(body)


def test_a_failed_measurement_is_a_bad_gateway_with_the_reason(client, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(api, "measure_with_real_model", lambda *a, **k: (None, "the profiling run printed no scores"))
    response = client.post("/api/compile-real", json={"source": EXAMPLE})
    assert response.status_code == 502 and "printed no scores" in response.json()["detail"]


def test_a_program_with_errors_or_no_compare_block_is_refused(client, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(api, "measure_with_real_model", lambda *a, **k: pytest.fail("must not measure"))
    assert client.post("/api/compile-real", json={"source": "agent x {"}).status_code == 400
    research = (EXAMPLES_DIR / "valid" / "research.af").read_text()
    response = client.post("/api/compile-real", json={"source": research})
    assert response.status_code == 400 and "no compare or necessity block" in response.json()["detail"]


def test_a_measurement_over_the_call_limit_is_refused(client, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(api, "MAX_REAL_CALLS", 5)
    monkeypatch.setattr(api, "measure_with_real_model", lambda *a, **k: pytest.fail("must not measure"))
    response = client.post("/api/compile-real", json={"source": EXAMPLE})     # 3 cases x 2 candidates = 6 calls
    assert response.status_code == 400 and "up to 6 model calls" in response.json()["detail"]


def test_only_one_measurement_runs_at_a_time(client, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(api, "measure_with_real_model", lambda *a, **k: pytest.fail("must not measure"))
    assert api.real_run.acquire(blocking=False)
    try:
        assert client.post("/api/compile-real", json={"source": EXAMPLE}).status_code == 409
    finally:
        api.real_run.release()


def test_the_normal_endpoint_never_measures(client, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(api, "measure_with_real_model", lambda *a, **k: pytest.fail("must not measure"))
    body = client.post("/api/compile", json={"source": EXAMPLE}).json()
    assert body["evaluation"][0]["mode"] == "mock"


def test_the_estimate_of_model_calls_alone_and_end_to_end():
    from compiler.pipeline import compile_source
    alone = compile_source(EXAMPLE)["evaluation"][0]
    assert alone["scope"] == "agent" and alone["model_calls"] == 6              # 3 cases x (1 + 1) candidate calls
    whole = compile_source((EXAMPLES_DIR / "valid" / "triage_end_to_end.af").read_text())["evaluation"][0]
    assert whole["scope"] == "end_to_end" and whole["model_calls"] == 8         # 4 cases x 2 candidates x 1 call each
