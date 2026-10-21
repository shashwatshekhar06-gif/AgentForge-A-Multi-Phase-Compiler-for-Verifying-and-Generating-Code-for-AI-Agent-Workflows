# AgentForge -- A Multi-Phase Compiler for Verifying and Generating Code for AI Agent Workflows

A compiler for a small language (`.af`) that describes multi-agent AI workflows. It structurally verifies the workflow before anything runs, then generates one runnable [LangGraph](https://github.com/langchain-ai/langgraph) Python file. A web UI shows the output of every compiler phase.

```
.af source → lexer → parser (AST) → semantic analysis → graph IR → verification → optimization → cost analysis → LangGraph .py
```

Compiler Design course project. Every phase is hand written in plain Python (standard library only, no parser generators).

![AgentForge: source, phases and the workflow graph](docs/slides_assets/03_graph_research.png)

## Why a compiler for this?

A multi-agent workflow is several AI steps ("agents") that share one state, with branches and loops, and every AI call costs money. Written directly in a runtime library, mistakes show up only while running. We wrote the same bugs directly in LangGraph 1.2.16 and ran them:

| Bug | Plain LangGraph | AgentForge |
|---|---|---|
| Edge to a misspelled node | error at `compile()` | AF-E204 |
| Route returns a typo | runs, silently stops | AF-W208 |
| Unreachable node | runs, never mentioned | AF-W301 |
| Node with no outgoing edge | runs, silently ends there | AF-E305 |
| Reading a field nobody wrote | `KeyError` at runtime, after paid AI calls | AF-E302 |
| Wrong type written | runs (`TypedDict` is not enforced) | AF-E203 |
| Endless loop | about 10,000 steps, each possibly a paid call | AF-E303 |
| Loop with no bound | nothing | AF-W308 |
| AI result never used | paid on every run | AF-W307 |
| Untrusted text reaching a refund tool | nothing | AF-W309 |
| Output not written on one path | caller gets an empty value | AF-E304 |

LangGraph catches 1 of these 11 before running. The compiler catches all of them before anything runs, with a line number and, where it applies, the exact path that causes the problem.

## What it checks

- **Syntax**, with error recovery (several errors per run)
- **Types and references**: undeclared tools or fields, type mismatches, wrong tool arguments, non-boolean conditions
- **Reachability**: dead agents, nodes that can never reach `end`
- **Cycles**: loops with no exit and no `max_iterations` bound
- **State completeness**: a field read before it is written on some path, an `output` field missing on some path to `end` (with a concrete witness path)

Three analyses go beyond the usual checks (details in `docs/novelty.md`):

- **Dead LLM call elimination** (liveness analysis): removes AI calls whose result is never read. On `wasted_llm_calls.af`, 7 calls become 4.
- **Static worst-case cost** (WCET-style): the maximum and minimum number of AI calls per run, computed before anything runs. A loop with no bound is flagged (AF-W308).
- **Prompt-injection taint analysis**: tools tagged `untrusted`, `sensitive` and `sanitizer`; untrusted text that can reach a sensitive tool gives AF-W309 with the exact chain.

These are structural checks. They do not predict what an LLM will answer.

## Results

- 210 automated tests, all passing, including 75 edge cases, mutation fuzzing, and a brute-force check of the cost analysis. Testing found 8 real bugs, all fixed (`docs/testing.md`).
- 4 valid examples compile and run (the largest, `insurance_claims.af`, is 129 lines). 13 invalid examples are each blocked or warned with their expected code. `loan_approval_blocked.af` (132 lines) reports 7 bugs in one compile and generates no code.
- A 3,000-agent workflow compiles in about 0.4 seconds.

Limitations: the AI and tools are mocks in the generated file, cost is counted in calls rather than tokens, taint analysis follows explicit data flow only, and LangGraph is the only target.

## Project layout

```
backend/
  compiler/       the compiler: tokens, lexer, parser, ast_nodes, diagnostics,
                  semantic, ir, verify, optimize, cost, taint, codegen, pipeline, cli
  api.py          FastAPI server (POST /api/compile, GET /api/examples)
  tests/          pytest suite (+ hand written AST fixtures)
frontend/         React + Vite + TypeScript UI (Monaco editor, React Flow graph, cost view)
examples/
  valid/          research.af, fraud.af, support_pipeline.af, insurance_claims.af
  invalid/        one file per bug class; the first line says which code it must produce
docs/
  contract.md     AST schema, API schema, diagnostic codes, design decisions
  grammar.md      the grammar in EBNF
  explained.md    plain-language explanation of every phase
  testing.md      how the compiler was tested, bugs found, results
  novelty.md      what is new (dead LLM call elimination, cost bounds, taint analysis)
  viva_prep.md    the whole project explained step by step, with likely questions
  slides_assets/  screenshots of the web UI
```

## Setup

Requirements: Python 3.11+ and Node 18+.

```bash
# backend
cd backend
python -m venv .venv
.venv\Scripts\activate            # Windows   (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt

# frontend
cd ../frontend
npm install
```

## Run the web UI

Two terminals:

```bash
# 1. API server (from backend/, with the venv active)
uvicorn api:app --reload --port 8000

# 2. UI (from frontend/)
npm run dev
```

Open http://localhost:5173. Pick an example from the dropdown or type your own, then press **Compile** (or Ctrl+Enter).

For the demo you can bookmark an example and a tab directly, for example
http://localhost:5173/?example=fraud.af&tab=Graph or http://localhost:5173/?example=read_before_write.af&tab=Verification.

## Use the command line

From `backend/`:

```bash
python -m compiler ../examples/valid/research.af -o workflow.py
python workflow.py                                   # runs in mock mode, no API keys needed
AGENTFORGE_CHOICE=conversational python workflow.py  # make the mock LLM pick another one_of option
```

Errors are printed like a classic compiler, with line, column, a caret and the witness path:

```
../examples/invalid/read_before_write.af:24:1: error AF-E302: agent 'responder' reads state.context, but on the path start -> classifier -> responder nothing writes it first
    path: start -> classifier -> responder
```

## Run the tests

```bash
cd backend
python -m pytest -q
```

The suite (210 tests) covers the lexer, parser, semantic analysis, each verification pass, the optimizer, cost and taint analysis, and codegen. It compiles every valid example and runs `py_compile` on the output, checks every invalid example produces exactly its expected codes, and (if `langgraph` is installed) runs the generated files.

## The language in one example

```
state AgentState {
  input query: string               # supplied by the caller
  context: list[string] = []        # default value
  output response: string           # must be written before end
  route: string
}

tool web_search(query: string) -> list[string]

agent classifier {
  state.route = llm("Classify the query", state.query) one_of ["factual", "conversational"]
}
agent researcher { state.context = web_search(state.query) }
agent responder  { state.response = llm("Answer using the context", state.query, state.context) }

flow main {
  start -> classifier
  classifier -[state.route == "factual"]-> researcher
  classifier -[else]-> responder
  researcher -> responder -> end
}
```

Bounded loops: `analyst -[not state.confident, max_iterations 3]-> analyst`.
See `docs/grammar.md` for the full grammar and `docs/contract.md` for all diagnostic codes.
