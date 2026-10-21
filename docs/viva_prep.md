# Viva preparation: AgentForge in depth

This guide is for answering in-depth questions. It covers:
1. the one-minute pitch;
2. what `.af` code means, construct by construct;
3. what LangGraph is, and what every part of the generated code does and why;
4. the compiler as the middleman: what it checks and transforms that neither side does;
5. one example traced through all 8 phases with the real outputs;
6. a question bank with answers;
7. a demo script and honest limitations.

`docs/explained.md` has the per-phase theory, and `docs/novelty.md` and `docs/testing.md` have the evidence.

---

## 1. The one-minute pitch

> Multi-agent AI systems are workflows: several LLM-powered "agents" pass a shared state between them, with branches and loops. Today people write them directly in Python frameworks like LangGraph. Python checks almost nothing about the *workflow*: a typo in a route, an agent reading data nobody wrote, a loop that never ends or a loop that silently burns money, untrusted text reaching a refund tool. All of these are found only at runtime, after paying for LLM calls, or never.
>
> AgentForge is a compiler for a small language, `.af`, that describes such a workflow. It runs the classic compiler pipeline: lexer, parser, semantic analysis, an intermediate graph representation, verification, optimization, and code generation. Its output is a runnable LangGraph program. Because the compiler understands the whole workflow before it runs, it can prove properties (every field is written before it is read, every path reaches the end), remove wasted LLM calls, bound the worst-case cost, and detect prompt-injection paths. **All of this happens at compile time, for free, before a single API call is paid for.**

The single sentence: **`.af` says *what* the workflow is, LangGraph is *how* it runs, and the compiler in between guarantees it is *correct, cheap and safe* before it runs.**

---

## 2. The `.af` language: what each construct means

A `.af` file has four kinds of declarations. Here is the full research example (`examples/valid/research.af`):

```
state AgentState {
  input query: string
  context: list[string] = []
  output response: string
  route: string
}

tool web_search(query: string) -> list[string]

agent classifier {
  state.route = llm("Classify the query as factual or conversational", state.query) one_of ["factual", "conversational"]
}
agent researcher { state.context = web_search(state.query) }
agent responder  { state.response = llm("Answer the query using the context", state.query, state.context) }

flow main {
  start -> classifier
  classifier -[state.route == "factual"]-> researcher
  classifier -[else]-> responder
  researcher -> responder
  responder -> end
}
```

### 2.1 `state`: the shared memory

| Construct | Meaning at runtime | What the compiler does with it |
|---|---|---|
| `state AgentState { ... }` | One record (dictionary) that all agents read from and write to. It is the **only** way agents communicate. | Builds the **state symbol table** (name → type, modifier, default). |
| `input query: string` | Given by the caller when the workflow starts. | Treated as *written at start* by definite assignment. |
| `input untrusted msg: string` | An input that comes from outside, such as a customer email. | A **taint source** for prompt-injection analysis. |
| `output response: string` | The result the caller gets back. | Must be written on **every** path to `end` (AF-E304). |
| `context: list[string] = []` | An internal field with a starting value. | Counts as *written at start*, so reading it is always safe. |
| `route: string` | An internal field with no default. | It **must** be written before any agent reads it (AF-E302). |

Types: `string`, `int`, `float`, `bool`, `list[T]`. They are checked statically (AF-E203).

### 2.2 `tool`: an external function

```
tool web_search(query: string) -> list[string]
tool send_email(to: string, body: string) -> bool sensitive
```

- A tool is a **declaration only**, like a function prototype in C. The real implementation is written in Python later, in the generated stub.
- The signature lets the compiler type-check every call (AF-E206).
- Optional tags are used by taint analysis:
  - `untrusted`: the result may contain attacker text, for example a web page;
  - `sensitive`: the tool has real-world effects, such as email, refunds or deletions;
  - `sanitizer`: the tool returns safe text, for example after validation, redaction or human approval.

### 2.3 `agent`: one step of the workflow

An agent is a block of statements that runs when the flow reaches it.

| Statement / expression | Meaning |
|---|---|
| `state.x = expr` | Write a field. |
| `llm("prompt", a, b)` | Ask a language model; the result is a `string`. This is the **expensive** operation. |
| `llm(...) one_of ["a", "b"]` | The model must answer with one of these values, which gives a typed, finite result. The compiler remembers the options, so a route typo like `"factul"` gives AF-W208. |
| `web_search(state.query)` | Call a declared tool. |
| `if (cond) { ... } else { ... }` | A local decision inside one agent. |
| `and`, `or`, `not`, `==`, `!=`, `<`, `>`, ... | Ordinary boolean and comparison operators. |

From each agent the compiler infers three sets, shown in the AST and Graph tabs:
- `reads`: every field it uses;
- `writes`: every field it may write;
- `must_writes`: the fields it writes on *every* path through its own `if`s (the intersection of the branches).

### 2.4 `flow`: the wiring between agents

| Edge | Meaning |
|---|---|
| `a -> b` | After `a`, always go to `b`. |
| `a -[cond]-> b` | Go to `b` if `cond` is true. The condition must be `bool` and must not call `llm` or a tool (AF-E207, AF-E211). |
| `a -[else]-> c` | Go here if no condition matched. |
| `a -[cond, max_iterations 3]-> a` | A loop edge that may be taken at most 3 times. This is what makes a loop bounded. |
| `start`, `end` | Built-in entry and exit nodes. |

Conditions on a node are tried **in source order**, and `else` comes last.

### 2.5 Why a new language instead of a Python library?

- **It is analysable.** `.af` has no arbitrary code, no imports and no aliasing. That is why the compiler can *prove* properties. In Python, a node is any function, so a tool cannot know which keys it reads or writes without running it, and in general this is undecidable.
- **It is small:** 25 keywords and about 48 token types in total, and the EBNF grammar is in `docs/grammar.md`. You can learn it in ten minutes.
- **It is portable.** The same `.af` could target another framework (CrewAI, AutoGen) with a new code generator, and the front and middle of the compiler would stay unchanged. This is the same reason real compilers have an IR.

---

## 3. LangGraph and the generated code

### 3.1 What LangGraph is

LangGraph (by LangChain) is a popular Python framework for running agent workflows as a **state machine**:
- `StateGraph(StateType)` creates a graph whose nodes share one state dictionary.
- `add_node(name, fn)` adds a node: a function that takes the state and returns the fields it updates.
- `add_edge(a, b)` adds a fixed transition.
- `add_conditional_edges(a, chooser, mapping)` lets a function choose the next node at runtime.
- `START` and `END` are the entry and exit.
- `graph.compile()` gives a runnable `app`. `app.invoke(state)` runs it to the end, and `app.stream(state)` yields each step.

It handles the *runtime*: scheduling, merging updates, streaming, checkpoints and persistence. It does **not** analyse what your functions do with the state.

### 3.2 Who the generated code is for

- **Developers** run it straight away. In mock mode it needs no keys. To make it real, they replace the `llm()` body and the tool stubs.
- **Teams** get one standard, readable file per workflow, with the same structure every time, and the comments point to the source line.
- **Production:** LangGraph is used in industry, so the output plugs into existing tooling (LangSmith tracing, checkpointing, deployment).
- **Our project:** it proves that the compiler is end to end. The tests execute the generated files and check the path taken (`test_codegen.py`, `test_examples.py`).

### 3.3 The generated file, section by section

This is the real output for `research.af`.

**1. Header and imports**
```python
"""Generated by AgentForge from research.af. Do not edit by hand; ..."""
import os
from typing import TypedDict
from langgraph.graph import END, START, StateGraph
MOCK_CHOICE = os.environ.get("AGENTFORGE_CHOICE")
```
`AGENTFORGE_CHOICE` lets you choose which `one_of` answer the mock LLM gives, so you can drive the run down a particular branch: `AGENTFORGE_CHOICE=conversational python out.py`.

**2. The state type: from `state AgentState {...}`**
```python
AgentState = TypedDict("AgentState", {
    "query": str,  # input
    "context": list[str],
    "response": str,  # output
    "route": str,
    # hidden fields added by the compiler (routing decisions and loop counters)
    "_next_classifier": str,
})
```
- Each `.af` type is translated: `string → str`, `list[string] → list[str]`.
- We use the **functional** `TypedDict("Name", {...})` form because the class form breaks on field names that are Python keywords, such as `class`. This was one of the bugs the edge-case tests found.
- **Hidden fields** start with `_`, which is why user names starting with `_` are reserved (AF-E213):
  - `_next_<node>` holds the routing decision of a branching node;
  - `_count_<i>` holds the loop counter for each `max_iterations` edge.

**3. The `llm` helper and the tool stubs: from the `tool` declarations**
```python
def llm(prompt, *context, one_of=None):
    # >>> A REAL MODEL CALL GOES HERE ...
    if one_of: ... return one_of[0]          # mock: first option, or AGENTFORGE_CHOICE
    return f"[mock LLM answer to: {prompt}]"

def tool_web_search(query: str) -> list[str]:
    """Tool 'web_search' (mock). Replace this body with the real implementation."""
    return ["[mock web_search result 1]", "[mock web_search result 2]"]
```
- Mocks mean the file runs **offline and for free**, which is ideal for testing and demos.
- Tools get a `tool_` prefix so a tool can never clash with Python builtins or our own helpers, for example a tool called `print` or `llm`.
- Tagged tools get warning comments, such as `# SENSITIVE: ...`.

**4. One node function per agent: from each `agent {...}`**
```python
def node_classifier(state: AgentState) -> dict:
    """Agent 'classifier' (line 12 of research.af)."""
    s = dict(state)  # work on a copy, return the updated state
    s["route"] = llm("Classify ...", s["query"], one_of=["factual", "conversational"])
    s["_next_classifier"] = route_classifier(s)
    return s
```
- `state.x` becomes `s["x"]`. Statements, `if` blocks and operators are translated one to one. Operator precedence is handled by inserting parentheses only where needed.
- **Why a copy?** LangGraph expects a node to *return* its updates, not change the input in place. Working on a copy keeps the semantics clean: later statements in the agent see earlier writes, just as in `.af`.
- The docstring gives the source line, so the code can be traced back to the `.af` file.
- **Dead code is already gone.** Dead agents (AF-W301) get no function, and dead LLM stores (AF-W307) are simply not emitted.

**5. Routing functions: from the conditional edges**
```python
def route_classifier(s) -> str:
    if s["route"] == "factual":
        return "researcher"
    return "responder"            # the else edge
```
With a loop bound (from `fraud.af`):
```python
def route_graph_analyst(s) -> str:
    # loop edge: taken at most 3 times
    if (not s["confident"]) and s["_count_0"] < 3:
        s["_count_0"] += 1
        return "graph_analyst"
    return "report_writer"
```
Without an `else`, the generated code raises a clear `RuntimeError`, and the compiler already warned about it (AF-W306).

**Key design decision: why is the router called *inside* the node and stored in `_next_<node>`?**
LangGraph's chooser function (the one passed to `add_conditional_edges`) **cannot change the state**. Its return value only picks the next node. But a `max_iterations` loop must *increment a counter* when it takes the loop edge, and that counter has to be stored in the state. So the node computes the decision itself, together with any counter update, saves it in `_next_<node>`, and the chooser just reads that field. This also guarantees that the condition sees the state *after* the agent ran, exactly as `.af` defines it.

**6. Graph wiring: from the `flow` block**
```python
graph = StateGraph(AgentState)
graph.add_node("classifier", node_classifier)
...
graph.add_edge(START, "classifier")
graph.add_conditional_edges("classifier", lambda s: s["_next_classifier"],
                            {"researcher": "researcher", "responder": "responder"})
graph.add_edge("researcher", "responder")
graph.add_edge("responder", END)
app = graph.compile()
```
- A node with one plain edge becomes `add_edge`.
- A branching node becomes `add_conditional_edges` with an explicit mapping, so LangGraph knows all possible targets and can draw the graph.

**7. The `__main__` block: a demo run**
```python
initial_state = {"query": "[sample query]", "context": [], "response": "", "route": "", "_next_classifier": ""}
for step in app.stream(initial_state, {"recursion_limit": 60}, stream_mode="updates"):
    ...
print("path:", "start -> classifier -> researcher -> responder -> end")
print("response:", ...)
```
- Every field gets a starting value: sample inputs, `.af` defaults, or empty values. Agents never read uninitialised data at runtime, and the compiler has already *proved* they never read meaningless data (definite assignment).
- **`recursion_limit` is computed from the program:** 50 + 2 × nodes × (sum of loop bounds + 1). A correct run never hits it. It is only a safety net.
- `stream` prints the path that was taken, so you can see the branching.

### 3.4 How each `.af` construct maps to LangGraph

| `.af` | Generated LangGraph / Python |
|---|---|
| `state S { ... }` | `S = TypedDict("S", {...})` plus hidden fields, and `initial_state` |
| `input` / `output` / default | comments in the TypedDict; sample, default or empty value in `initial_state` |
| `tool t(...) -> T` | `def tool_t(...) -> T:` mock stub |
| `agent a { ... }` | `def node_a(state)` plus `graph.add_node("a", node_a)` |
| `state.x = e` | `s["x"] = <e in Python>` |
| `llm(p, a) one_of [..]` | `llm(p, a, one_of=[..])` |
| `if / else` | Python `if / else` |
| `a -> b` | `graph.add_edge("a", "b")` |
| `a -[c]-> b`, `a -[else]-> d` | `route_a()` plus `_next_a` plus `add_conditional_edges` |
| `max_iterations N` | a `_count_i` field plus the guard `and s["_count_i"] < N` |
| `start`, `end` | `START`, `END` |
| dead agent / dead store | nothing (removed by the optimizer) |

---

## 4. The compiler as the middleman

### 4.1 What LangGraph alone catches: we tested it

We wrote the same bugs directly in LangGraph 1.2.16, the version installed in our `.venv`, and ran them:

| Bug written directly in LangGraph | What LangGraph does | What AgentForge does |
|---|---|---|
| Edge to a misspelled node (`"xx"`) | ✅ error at `compile()` | AF-E204 |
| **Conditional route returns a typo** (`"factul"`) | ❌ **runs**, prints a warning to the log and silently stops the run | AF-W208, using the `one_of` values |
| **Unreachable node** | ❌ runs, never mentioned | AF-W301, and the node is removed from the output |
| **Node with no outgoing edge** | ❌ runs and silently ends there | AF-E305 |
| **Reading a key nobody wrote** | ❌ `KeyError` at **runtime**, after earlier LLM calls were paid for | AF-E302, with a witness path |
| **Writing the wrong type** (`42` into a `str`) | ❌ runs: TypedDict is **not enforced** at runtime | AF-E203 |
| **Endless loop** | ❌ runs until `GraphRecursionError` after 10,007 steps (the default limit in this version), **every step a possible paid LLM call** | AF-E303, at compile time |
| Loop with no bound | ❌ nothing | AF-W308, with a worst-case cost of "unbounded" |
| LLM result never used | ❌ nothing; you pay for it on every run | AF-W307, and the call is removed |
| Untrusted email reaching `issue_refund` | ❌ nothing | AF-W309, with the chain |
| An output not written on one path | ❌ the caller gets an empty value | AF-E304, with a witness path |

**Only 1 of these 11 problems is caught by LangGraph.** The compiler catches all of them before anything runs. That is the core argument for the project.

Why can't LangGraph do this itself? Its nodes are arbitrary Python functions. Knowing which state keys a Python function reads, or whether it always writes a key, is undecidable in general (Rice's theorem). `.af` is designed so that these questions **are** answerable: every read and write is a visible `state.x`, and there are no pointers, aliases or dynamic keys.

### 4.2 The middleman's three jobs

1. **Check (analysis).** It rejects or warns about workflows that are wrong, wasteful or unsafe, using classic compiler analyses: symbol tables, type checking, reachability, Tarjan SCC, definite assignment, liveness, WCET-style bounds and taint.
2. **Transform (optimization).** It removes dead agents and dead LLM calls, so the program that runs is *cheaper* than the one that was written, without changing its result.
3. **Translate (code generation).** It lowers high-level concepts that LangGraph does not have into LangGraph primitives:
   - `max_iterations` becomes counters and guards;
   - `else` becomes the fall-through branch of the router;
   - `one_of` becomes the mock's options and the compiler's type facts;
   - conditions evaluated after the agent become `_next_<node>`.

This is exactly what a C compiler does between C and machine code: check, optimize, translate.

### 4.3 The analogy to a real compiler

| Real compiler (C → x86) | AgentForge (`.af` → LangGraph) |
|---|---|
| Source program | `.af` workflow |
| Tokens, AST | the same: tokens, AST |
| Symbol table, type checker | the same: state, tool and agent tables; types |
| IR: control-flow graph of basic blocks | IR: workflow graph of agents (each agent is like a basic block) |
| "variable used before initialisation" warning | AF-E302 read before write |
| unreachable code elimination | dead agent elimination |
| dead store elimination | dead LLM call elimination |
| WCET (worst-case execution time) analysis for real-time systems | worst-case LLM *cost* analysis |
| taint analysis (SQL injection, `-Wformat-security`) | prompt-injection analysis |
| Assembly / machine code | LangGraph Python |

The difference is what we optimize. Real compilers save CPU **cycles**. We save LLM **calls**, which cost about a million times more (cents versus nanoseconds). So the classic techniques pay off far more here.

---

## 5. One example through all 8 phases (`research.af`, real outputs)

1. **Lexer → 122 tokens.** The longest-match scanner reads the file character by character: `state` → `STATE` (keyword), `AgentState` → `IDENT`, `{` → `LBRACE`, `list` → `LIST_TYPE`, and so on. Comments and whitespace are dropped. Every token keeps its line and column for error messages.
2. **Parser → AST.** Recursive descent, one function per grammar rule (`parse_state`, `parse_agent`, `parse_expr`, ...), using one token of lookahead (LL(1)). The result is a `Program` with 1 `StateDecl` (4 fields), 1 `ToolDecl`, 3 `AgentDecl`s and 1 `FlowDecl` (5 edges).
3. **Semantic → symbol tables.** All names resolve and all types check. The inferred sets:
   - classifier reads {query} and writes {route};
   - researcher reads {query} and writes {context};
   - responder reads {query, context} and writes {response}.
4. **IR → graph.** 5 nodes (start, 3 agents, end) and 5 edges, one of them an `else` edge. Each node carries its LLM and tool call counts.
5. **Verify → 7 checks, all passing:**
   - reachability: every agent is reachable;
   - SCC: no cycles;
   - backward reachability: everything reaches `end`;
   - definite assignment: `context` has a default, so it is safe on the conversational path. Without that default this is exactly `read_before_write.af`, which gives AF-E302.
   - outputs: `response` is written on both paths;
   - else edges: present;
   - taint: no sensitive tools, so there is nothing to protect.
6. **Optimize.** Nothing is dead, so nothing is removed. Compare `wasted_llm_calls.af`, where 7 calls become 4.
7. **Cost.** Worst case is 2 LLM calls and 1 tool call, on the factual path. Best case is 2 LLM calls and 0 tool calls.
8. **Codegen → the file in section 3**, which runs and prints `path: start -> classifier -> researcher -> responder -> end`.

---

## 6. Question bank

### The project and its motivation
- **Q: Why is this a compiler and not just a code template or translator?**
  A: A translator maps syntax. A compiler *understands* the program: it builds symbol tables and an IR, runs dataflow analyses, rejects incorrect programs, and optimizes. Two thirds of our code (semantic, verify, optimize, cost, taint) is analysis, not translation.
- **Q: Who would use this?**
  A: Teams building agent systems who want errors and cost known before deploying, and anyone who wants a readable spec of a workflow that is guaranteed to match the code.
- **Q: Why LangGraph as the target?**
  A: It is the most widely used graph-based agent runtime, and its model (shared state, nodes, conditional edges) matches our IR almost one to one, so codegen stays simple and readable.
- **Q: Why not have an LLM generate the LangGraph code?**
  A: An LLM gives no guarantees. It can produce exactly the bugs in 4.1. Our compiler is deterministic and gives proofs (for example, every field is written before it is read on *every* path) and repeatable output. It also costs nothing per compile.

### Lexer and parser
- **Q: Why hand-write the lexer and parser instead of using a tool like ANTLR?**
  A: The spec required it, and it gives full control over error messages and recovery. The grammar is LL(1), so recursive descent is natural.
- **Q: What is LL(1)?**
  A: The parser reads Left to right, builds a Leftmost derivation, and needs 1 token of lookahead to decide which rule to apply. Our grammar has no left recursion and no ambiguity. Precedence is encoded as layers: `or` → `and` → `not` → comparison → primary.
- **Q: How do you report several syntax errors at once?**
  A: Panic-mode recovery. On an error we record it, skip tokens until a synchronising token (the start of the next `agent`, `tool`, `state` or `flow`), and continue. `syntax_errors.af` shows several errors in one compile.
- **Q: What is AF-E103?**
  A: A nesting limit of 100. Deeply nested input such as `((((...))))` would otherwise overflow Python's recursion in the recursive parser. The fuzzer found that crash, and now it is a clean error.

### Semantic analysis
- **Q: What is in your symbol tables?**
  A: Three tables:
  - fields: name, type, input/output, default, untrusted;
  - tools: parameters, return type, tags;
  - agents: reads, writes, must_writes.
- **Q: What is `must_writes` and why intersect?**
  A: The fields an agent writes on *every* path through its own `if/else`. For `if (c) { x = 1 } else { y = 2 }`, neither x nor y is guaranteed, so must_writes = {x} ∩ {y} = ∅.
- **Q: How does `one_of` help?**
  A: If a field is only ever set by `llm(...) one_of [...]`, its possible values are known. A comparison with any other value can never be true, which gives AF-W208. That catches route typos, which LangGraph silently ignores.

### IR and verification
- **Q: Why an IR at all? Why not verify the AST?**
  A: The AST mirrors the *text*. The IR is the *control flow*: nodes and edges with their read/write facts. Graph algorithms (BFS, Tarjan, dataflow) work on graphs. It also decouples the front end from codegen, so a new target only needs a new back end.
- **Q: How do you detect endless loops?**
  A: Tarjan's algorithm finds strongly connected components in O(V+E). An SCC is an *endless cycle* if no edge leaves it and none of its edges has `max_iterations`.
- **Q: Explain definite assignment.**
  A: It is a forward dataflow analysis. IN[n] is the intersection of OUT[p] over all predecessors p, and OUT[n] = IN[n] ∪ must_writes[n]. start begins with the inputs and defaults, and every other node starts at "all fields", the top of the lattice. Iterating until nothing changes reaches the greatest fixpoint. If an agent reads a field that is not in IN, that is AF-E302.
- **Q: Why intersection?**
  A: The question is "written on *all* paths?". If any one path misses the field, the read is unsafe.
- **Q: Why does the fixpoint terminate?**
  A: The sets can only shrink and are finite (there are finitely many fields), so it takes at most |fields| × |nodes| changes.
- **Q: What is a witness path?**
  A: A concrete path from start to the failing node where the field is never written, highlighted in the graph. It turns "an error exists" into "here is exactly how it happens".

### Optimization and cost (novelty)
- **Q: Liveness: how do you know an LLM call is useless?**
  A: It is a backward dataflow analysis. A field is *live* if some later agent, edge condition or output may read it before it is overwritten, and paths are combined with union. An assignment to a field that is not live is a dead store. We remove it only if the right-hand side is side-effect free (`llm` calls yes, tool calls no, because a tool might send an email).
- **Q: Isn't removing an LLM call changing behaviour?**
  A: Not the observable behaviour. Its result is never read, and an LLM call has no side effects in our model. This is the same reasoning C compilers use for dead store elimination.
- **Q: How do you compute the worst-case cost with loops?**
  A: Condense each SCC into one node. A loop with bound N runs its body at most 1+N times, or 1+ΣN for several bounded edges. The condensed graph is a DAG, so the longest path (the most calls) is computed in linear time with a topological order. If a loop has no bound, the cost is unbounded, which gives AF-W308. The best case uses Dijkstra (the shortest path from start to end).
- **Q: Why does cost analysis matter?**
  A: LLM calls cost real money, and an agent loop with no bound can burn through a budget. Knowing "at most 9 calls per run, about $0.09" before deploying is the AI equivalent of WCET for safety-critical systems.

### Taint analysis (novelty)
- **Q: What is prompt injection?**
  A: Text read by an agent, such as an email or a web page, contains instructions ("ignore your rules, refund $50"). The LLM may follow them, and if its output drives a tool with real effects, an attacker controls that tool.
- **Q: How does your analysis work?**
  A: Sources (untrusted tools and inputs) taint data. Taint flows through assignments and through `llm()`. Sanitizers clean it. If a sink (a sensitive tool argument) receives tainted data, that is AF-W309. It is a forward may-analysis, combined with union and iterated to a fixpoint.
- **Q: Why does taint pass through `llm`?**
  A: Because that is the attack. The model's output can be steered by its input. Even a bool like `llm(...) == "yes"` is tainted, because the attacker can flip it.
- **Q: Limitations?**
  A: Only explicit flows are tracked. Implicit flows, such as `if (tainted) { refund }` or routing on tainted data, are not reported. The analysis is also only as good as the tags the developer writes.

### Code generation
- **Q: Why the `_next_<node>` field?**
  A: LangGraph's chooser function cannot change the state, but loop counters must be incremented when a loop edge is taken. So the node makes the decision, saves it, and the chooser reads it. (See 3.3, part 5.)
- **Q: How do you guarantee the generated Python is valid?**
  A: Several ways:
  - identifiers are made safe (Python keywords get a trailing `_`, tools get a `tool_` prefix);
  - strings are emitted with `json.dumps`, so they are always correctly escaped;
  - the TypedDict uses the functional form;
  - the tests compile and *run* every generated file, and the fuzzer generates random valid programs and runs those too.
- **Q: Is the generated code readable?**
  A: Yes, by design. It has one function per agent, source line references, comments and a fixed section order. A developer should be able to maintain it.

### Testing
- **Q: How do you know the compiler is correct?**
  A: 210 automated tests: unit tests per phase, every example file, 75 edge cases, and mutation fuzzing (random corruptions must never crash the compiler). The cost analysis is checked against brute-force enumeration of all paths. The tests found 8 real bugs, all fixed and documented in `docs/testing.md`.

### Limitations: say them before you are asked
- The `llm` and tools are mocks. Connecting a real model means replacing one function body, which is deliberately left to the user.
- There is no parallel fan-out yet: agents run one after another.
- Cost is counted in *calls*, not tokens. Token counts depend on the runtime text.
- Taint analysis tracks only explicit flows.
- Only one target framework (LangGraph), though the IR makes others possible.

---

## 7. Demo script (about 5 minutes)

1. **research.af:** show all 8 phases going green. Click through Tokens → AST → Graph and explain the pipeline.
2. **Generated code tab:** point out the TypedDict, a node function, a router and the wiring. Run it in the terminal: `python -m compiler ../examples/valid/research.af -o out.py` and then `python out.py`.
3. **read_before_write.af:** the AF-E302 error, the witness path highlighted in red, and "Why?". *"LangGraph would crash with KeyError at runtime, after paying for the classifier."*
4. **wasted_llm_calls.af:** the Cost tab shows 7 → 4 calls and the money saved.
5. **unbounded_cost.af:** worst case is "unbounded" (AF-W308). Add `max_iterations 3` live and watch it become a number.
6. **prompt_injection.af:** two AF-W309 chains, and the sanitized reply is accepted.
7. **loan_approval_blocked.af:** 7 bugs in one compile; codegen is blocked and nothing is generated.

