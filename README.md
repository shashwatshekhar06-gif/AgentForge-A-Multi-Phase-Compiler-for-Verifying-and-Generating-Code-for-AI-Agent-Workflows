# AgentForge

A compiler for a small language (`.af`) that describes multi-agent AI workflows. It structurally verifies the workflow before anything runs, then generates one runnable [LangGraph](https://github.com/langchain-ai/langgraph) Python file. A web UI shows the output of every compiler phase.

```
.af source → lexer → parser (AST) → semantic analysis → graph IR → verification → optimization → cost analysis → LangGraph .py
```

Compiler Design course project. Every phase is hand written in plain Python (standard library only, no parser generators).

## What it checks

- **Syntax**, with error recovery (several errors per run)
- **Types and references**: undeclared tools or fields, type mismatches, wrong tool arguments, non-boolean conditions
- **Reachability**: dead agents, nodes that can never reach `end`
- **Cycles**: loops with no exit and no `max_iterations` bound
- **State completeness**: a field read before it is written on some path, an `output` field missing on some path to `end` (with a concrete witness path)

It also detects **prompt injection** risks, where untrusted text (a web page, an email) can reach a tool with real-world effects, using taint analysis. And it **saves money**: it removes LLM calls whose results are never used (liveness analysis) and computes, before anything runs, the worst-case and best-case number of LLM calls per run. It warns when a loop has no cost ceiling. See `docs/novelty.md`.

These are structural checks. They do not predict what an LLM will answer.

## Project layout

```
backend/
  compiler/       the compiler: tokens, lexer, parser, ast_nodes, diagnostics,
                  semantic, ir, verify, optimize, codegen, pipeline, cli
  api.py          FastAPI server (POST /api/compile, GET /api/examples)
  tests/          pytest suite (+ hand written AST fixtures)
frontend/         React + Vite + TypeScript UI (Monaco editor, React Flow graph)
examples/
  valid/          research.af, fraud.af, support_pipeline.af
  invalid/        one file per bug class; the first line says which code it must produce
docs/
  contract.md     AST schema, API schema, diagnostic codes, design decisions
  grammar.md      the grammar in EBNF
  explained.md    plain-language explanation of every phase (viva notes)
  testing.md      how the compiler was tested, bugs found, results
  novelty.md      what is new (dead LLM call elimination, static cost bounds) and the savings
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

The suite covers the lexer, parser, semantic analysis, each verification pass and codegen. It compiles every valid example and runs `py_compile` on the output, checks every invalid example produces exactly its expected codes, and (if `langgraph` is installed) runs the generated files.

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
