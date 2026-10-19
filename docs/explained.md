# AgentForge explained (read this before the viva)

AgentForge is a compiler. Its input is a small language (`.af` files) that describes a team of AI agents: what data they share, what tools they can call, what each agent does, and in what order they run. Its output is one Python file that runs the workflow with the LangGraph framework.

Like any compiler, it works in **phases**. Each phase takes the previous phase's output and produces something more structured:

```
source text --lexer--> tokens --parser--> AST --semantic--> checked AST + symbol table
   --lowering--> graph IR --verify--> report --optimize--> smaller IR --codegen--> Python file
```

All phases are hand written in plain Python (no parser generators). The code is in `backend/compiler/`.

A running example is used throughout. It is the research assistant in `examples/valid/research.af`:

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

What the compiler promises, honestly: it **structurally verifies** the workflow (syntax, types, names, reachability, loops, whether data is available when it is read). It cannot know what an LLM will answer, so it does not claim the workflow is "correct" in every sense.

---

## 1. Lexical analysis (`tokens.py`, `lexer.py`)

**What it does.** Reads the source one character at a time and groups characters into *tokens*: keywords (`agent`, `flow`), names (`classifier`), literals (`"factual"`, `3`), and symbols (`{`, `->`, `==`). It skips spaces and `#` comments, and remembers the line and column of every token so later errors can point at the right place.

**Compiler concept.** A lexer (scanner) is a hand coded *finite state machine*: the current character decides what kind of token we are in (word, number, string, symbol). It uses **longest match**: `==` becomes one token, not two `=` tokens, and `->` is one token, not `-` then `>`.

**Error handling.** A bad character (`@`) gives `AF-E001`; a string without a closing quote gives `AF-E002`. The lexer records the error, skips the character, and keeps going, so it can report several errors in one run.

**Tiny example.** `classifier -[else]-> responder` becomes

```
IDENT(classifier)  EDGE_OPEN(-[)  ELSE(else)  RBRACKET(])  ARROW(->)  IDENT(responder)
```

---

## 2. Parsing (`parser.py`, `ast_nodes.py`, grammar in `docs/grammar.md`)

**What it does.** Checks that the tokens follow the grammar, and builds an **abstract syntax tree (AST)**: a tree of objects such as `AgentDecl`, `Assign`, `LlmCall`, `Edge`. "Abstract" means punctuation like `{` `}` and `;` is gone; only the meaning is kept.

**Compiler concept.** **Recursive descent parsing.** Every grammar rule is one function (`parse_agent_decl`, `parse_if`, `parse_expr`, ...). A function looks at the next token (one token of *lookahead*) to choose what to do, and calls the functions for the smaller rules inside it. The grammar is **LL(1)**: one token of lookahead is always enough to decide.

**Operator precedence** is handled with one function per level, from the loosest to the tightest:
`or` → `and` → `not` → comparison (`==`, `<`, ...) → primary (literals, `state.x`, calls, parentheses).
So `not state.a == 1 and state.b` is read as `(not (state.a == 1)) and state.b`.

**Error recovery (panic mode).** When the parser finds a wrong token it records `AF-E101` ("expected X but found Y"), or `AF-E102` for a missing `}` `]` `)`. It then skips tokens until the start of the next declaration (`agent`, `tool`, `flow`, `state NAME`) and continues. One run therefore reports several syntax errors. See `examples/invalid/syntax_errors.af`.

**Tiny example.** `state.context = web_search(state.query)` becomes

```
Assign
├── target: StateRef(field="context")
└── value:  Call(name="web_search")
            └── args[0]: StateRef(field="query")
```

Edge chains are sugar: `a -> b -> c` becomes two `Edge` nodes.

---

## 3. Semantic analysis (`semantic.py`)

**What it does.** The parser only checks *shape*. Semantic analysis checks *meaning*:

- **Symbol tables**: it collects every state field (with its type), every tool (with its parameter and return types), and every agent. Duplicates give `AF-E205`.
- **Reference validation**: every `state.x` must be a declared field (`AF-E202`), every called tool must exist (`AF-E201`), every name in the flow must be an agent, `start` or `end` (`AF-E204`).
- **Type checking**: each expression gets a type. `llm(...)` is a `string` (or the type of its `one_of` options), a tool call has the tool's return type, a comparison is a `bool`. Assigning a string to a `list[string]` field gives `AF-E203`. Tool arguments are checked in number and type (`AF-E206`). Every condition must be a `bool` (`AF-E207`).
- **Flow rules**: no edge into `start` or out of `end` (`AF-E210`), edge conditions may only read state, not call tools (`AF-E211`), and each node must have either one plain edge, or conditional edges plus at most one `else` (`AF-E212`).
- **Read/write inference**: for each agent it works out which fields it **reads** (before writing them itself), which it **may write**, and which it **must write** (written on every path through its body; a write inside only one branch of an `if` does not count).
- **one_of check**: if `route` is only ever set by `llm(...) one_of ["factual", "conversational"]`, then the edge `state.route == "factul"` can never be true, so it warns `AF-W208`.

**Compiler concept.** Symbol tables, static type checking, and name resolution, the same as in a C or Java compiler.

**Tiny example.** For `responder`: reads = `[query, context]`, writes = `[response]`, must_writes = `[response]`.

---

## 4. Lowering to the graph IR (`ir.py`)

**What it does.** Turns the checked program into an **intermediate representation (IR)**: a directed graph. Every agent is a node, plus two special nodes `start` and `end`. Every transition is an edge that carries its condition, whether it is the `else` edge, and its optional `max_iterations` bound. Each node also carries the agent's reads, writes and must-writes.

**Compiler concept.** An IR is the form a compiler analyses and optimizes, separate from the source syntax. Real compilers use a **control flow graph**; our IR is exactly that, with agents in place of basic blocks.

**Tiny example.** The research workflow becomes 5 nodes (`start, classifier, researcher, responder, end`) and 5 edges, one of them labelled `state.route == "factual"` and one marked `else`.

---

## 5. Verification passes (`verify.py`)

Each pass is its own function with its own tests. They run on the IR graph.

### 5.1 Dead agents: reachability analysis
A **breadth first search** from `start` finds every node that can run. An agent that is never reached is dead: warning `AF-W301`.
A second search runs **backwards from `end`**. A reachable node that cannot reach `end` would get stuck: error `AF-E305`.

### 5.2 Cycle detection: strongly connected components
**Tarjan's algorithm** splits the graph into *strongly connected components* (SCCs): groups of nodes where every node can reach every other one. Every cycle lies inside one SCC. A cycle is accepted if one of its edges has a `max_iterations` bound **or** one edge leaves the cycle. Otherwise the workflow could loop forever: error `AF-E303`, with the cycle path. (We do not claim to prove termination in general; this is a structural check.)

### 5.3 State completeness: definite assignment (dataflow analysis)
The question: when an agent reads `state.x`, has `x` **surely** been written on **every** path from `start`?

For each node we compute `IN[n]`, the set of fields written on every path to `n`:
- `IN[start]` = the `input` fields and fields with a default.
- `OUT[n]` = `IN[n]` plus the fields `n` must write.
- `IN[n]` = the **intersection** of `OUT` over all predecessors of `n` (a field counts only if *every* incoming path wrote it).

Loops make this circular, so we iterate: start every node at "all fields" and keep recomputing until nothing changes (a **fixpoint**). Sets only ever shrink and are finite, so this always stops.

Then:
- an agent reading a field not in `IN[n]` gives `AF-E302`,
- an edge condition reading a field not in `OUT[n]` gives `AF-E302`,
- an `output` field not in `IN[end]` gives `AF-E304`.

Each error comes with a **witness path**: a real path from `start` on which nothing writes the field (found with a BFS that avoids the nodes that write it).

**Tiny example.** In `examples/invalid/read_before_write.af` (the spec's original example, where `context` has no default):
`IN[responder]` = `OUT[classifier] ∩ OUT[researcher]` = `{query, route} ∩ {query, route, context}` = `{query, route}`.
`responder` reads `context`, which is not in the set, so we get `AF-E302` with witness path `start -> classifier -> responder`.
The fix is `context: list[string] = []`.

### 5.4 Else edges
A node with conditional edges but no `else` edge gets warning `AF-W306`. If no condition matches at runtime, the generated code raises a clear error.

---

## 6. Optimization: dead agent elimination (`optimize.py`)

**What it does.** Removes unreachable agents (and their edges) from the IR before code generation, and lists them in `removed_agents`. No code is generated for an agent that can never run.

**Compiler concept.** This is **dead code elimination**, done on the workflow graph instead of on instructions.

**Tiny example.** In `examples/invalid/unreachable_agent.af`, `summarizer` has no incoming edge. Verification warns `AF-W301`, and the generated file has no `node_summarizer`.

---

## 7. Code generation (`codegen.py`)

**What it does.** Walks the optimized IR and prints one Python file:

1. `class AgentState(TypedDict)`: one entry per state field, plus hidden fields the compiler adds (`_next_<node>` for routing decisions, `_count_<i>` for loop counters).
2. `llm(...)` and one `tool_<name>(...)` function per tool. They run in **mock mode** and return placeholder values, so the file runs with no API keys. A comment marks where a real model call would go. For `one_of`, the mock returns the first option, or the one named in the `AGENTFORGE_CHOICE` environment variable.
3. One `node_<agent>(state)` function per agent, with the statements translated to Python (`state.x` becomes `s["x"]`).
4. One `route_<node>(s)` function per branching node. It tries the conditions in source order and falls back to the `else` target. For a `max_iterations N` edge it also checks and increments a counter, so the loop edge is taken at most `N` times, after which routing falls through to the other edges.
5. The wiring: `StateGraph(AgentState)`, `add_node`, `add_edge`, `add_conditional_edges`, and `graph.compile()`.
6. A `__main__` block that runs the workflow once and prints the path taken and the output fields.

**Compiler concept.** **Code generation** (target code emission) from the IR, together with simple *runtime support* (the hidden counters, like a compiler inserting a loop counter variable).

**Tiny example.** The branch after `classifier` becomes

```python
def route_classifier(s) -> str:
    if s["route"] == "factual":
        return "researcher"
    return "responder"
```

and running it prints `path: start -> classifier -> researcher -> responder -> end`.

---

## 8. Pipeline, API and UI

`pipeline.py` runs the phases in order. If a phase reports an error, later phases are marked `skipped`, but the output of the earlier phases is still returned, so the UI can show the tokens and the AST even when type checking fails. `api.py` exposes this as `POST /api/compile`. The React UI (`frontend/`) shows each phase in its own tab: tokens, AST, workflow graph, verification report and generated code. Errors appear as red squiggles in the editor.

## Likely viva questions

- *Why recursive descent?* It is simple, readable, needs no generator tool, and gives good error messages. Our grammar is LL(1), so it fits naturally.
- *Why intersection in the dataflow?* We need fields written on **every** path. A field written on only some paths is not safe to read (that would be union, which answers "may be written").
- *Why does the fixpoint terminate?* The sets start full, can only shrink, and there are finitely many fields.
- *Why SCCs for cycles?* Every cycle is inside one SCC, so checking each SCC once (bounded? has an exit?) covers all cycles, including nested ones, in linear time.
- *Is dead agent elimination safe?* Yes: an unreachable node can never execute, so removing it cannot change behaviour.
- *Does verification guarantee the workflow works?* No. It checks the structure (names, types, reachability, loops, data availability). It cannot predict LLM output.
