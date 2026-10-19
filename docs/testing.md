# Testing AgentForge

How the compiler was tested, what testing found, and how to reproduce every result.

**Summary:** 167 automated tests, all passing in about 28 seconds. Edge case testing, fuzzing and differential testing found **8 real bugs**, all fixed. After the fixes, more than 4,000 random and corrupted programs never made the compiler crash, and the verifier agreed exactly with an independent brute force checker on 480 random workflows.

Environment: Python 3.12.6, LangGraph 1.2.16, Windows 11.

---

## 1. How to run the tests

```bash
cd backend
python -m pytest -q                       # everything
python -m pytest -q tests/test_fuzz.py    # only the fuzz and differential tests
```

All random tests use fixed seeds, so every run produces exactly the same programs and results. Tests that execute the generated Python are skipped automatically if `langgraph` is not installed.

## 2. Test suite overview

| File | Tests | What it covers |
|---|---:|---|
| `test_lexer.py` | 7 | tokens, longest match (`==`, `->`, `-[`), line/column tracking, escapes, multiple lexical errors |
| `test_parser.py` | 12 | AST shape, operator precedence, edge chains, `else if`, panic mode recovery, AST JSON round trip |
| `test_semantic.py` | 17 | every semantic error code, read/write/must-write inference; also runs from a hand written AST fixture |
| `test_verify.py` | 13 | each verification pass on its own: reachability, SCC cycles, dataflow fixpoint, witness paths, else edges |
| `test_codegen.py` | 7 | generated file structure, loop counters, dead agent removal, runs the generated workflows |
| `test_examples.py` | 14 | every valid example compiles and passes `py_compile`; every invalid example gives exactly its expected codes |
| `test_api.py` | 3 | the `/api/compile` and `/api/examples` endpoints match the contract |
| `test_edge_cases.py` | 75 | edge cases in every phase, including 8 tests that run unusual generated programs under LangGraph |
| `test_fuzz.py` | 19 | mutation fuzzing, random garbage input, differential testing of the verifier |
| **Total** | **167** | |

## 3. Testing methods

### 3.1 Unit and example tests
Each phase is tested on its own with small hand written inputs. The back half (semantic analysis to codegen) can be tested from a hand written AST JSON fixture, without the lexer or parser. Every example in `examples/invalid/` starts with a line such as `# expect: AF-E302`, and a test checks that the compiler produces **exactly** that set of codes, nothing more and nothing less.

### 3.2 Edge case tests
Unusual inputs chosen by hand to probe the boundaries of each phase, for example:

- **Lexer:** empty file, comment-only file, Windows line endings, tabs, Unicode inside strings, Unicode outside strings, huge integers, unknown escapes, `-` or `!` on their own.
- **Parser:** 17 different truncated programs (`agent x {`, `flow f { start -[`, ...), an empty edge label, chained comparisons, `else` with `max_iterations`, 20 broken declarations in one file, nesting at and beyond the limit.
- **Semantic:** reserved names, an agent and a tool with the same name, nested list types, bad defaults, mixed `one_of` types, errors that must not cascade.
- **Verification:** `start -> end` only, no edges at all, an unconditional self loop, a bounded loop with no exit, a dead cycle, a field written only inside a loop, `else if` chains, two separate loops.
- **Code generation:** Python keywords as field, parameter and agent names; quotes, backslashes and braces in prompts; an empty state; branching from `start`.
- **Runtime:** the generated file is actually executed for keyword names, branching from `start`, a bounded loop from `start`, an empty state, `AGENTFORGE_CHOICE` with integer options, two loops that must each stop at their own bound, the clear error when no edge matches, and a 60-agent chain.

### 3.3 Mutation fuzzing
The 12 example programs are corrupted at random: characters deleted or inserted, files truncated, tokens deleted, inserted or swapped, whole lines deleted or duplicated, and names swapped for other names in the program. Pure random token soup is added on top. For **every** input these invariants are checked:

1. the compiler does not raise an exception,
2. every diagnostic has a valid severity, phase, line ≥ 1, column ≥ 1, a message, and a code listed in `docs/contract.md`,
3. `ok` is true exactly when there is no error,
4. after the first failed phase, all later phases are `skipped`,
5. if the program compiled, the generated Python is syntactically valid.

| | Count |
|---|---:|
| mutated example programs | 2,500 |
| random token-soup programs | 1,500 |
| mutants failing in the lexer / parser / semantic / verify phase | 104 / 1,907 / 332 / 64 |
| mutants that still compiled | 93 |
| distinct diagnostic codes triggered | 19 |
| invariant violations (after the fixes) | **0** |

### 3.4 Differential testing of the verifier
The verifier is the most algorithmic part of the compiler, so it is checked against a second, deliberately different implementation. The test generates **480 random workflows**, all well typed: 1 to 6 agents, 1 to 5 fields, random defaults and outputs, and writes that are definite, inside only one branch, or in both branches. Edges are random too: plain, conditional, bounded and `else`, sometimes with dead ends.

For each workflow the expected results are computed by **brute force**:

| Compiler (`verify.py`) | Brute force checker (in the test) |
|---|---|
| dataflow fixpoint with intersection | enumerate **every** simple path from `start` and intersect the writes along each |
| Tarjan's strongly connected components | mutual reachability from a separate depth first search started at every node |
| BFS forwards and backwards | reachability sets computed per node |

(Enumerating simple paths is enough: writes only add fields, so a path with a loop writes at least as much as the same path with the loop removed.)

The two must agree **exactly** on the dead agents (W301), every (agent, field) read-before-write pair (E302), missing outputs (E304), the number of endless cycles (E303), the nodes that cannot reach `end` (E305) and missing `else` edges (W306). In addition, **every witness path** is checked to be a real path from `start` on which no node writes the field.

| Diagnostic | Times checked |
|---|---:|
| AF-W301 dead agent | 785 |
| AF-E302 read before write | 503 |
| AF-E305 cannot reach end | 267 |
| AF-E304 missing output | 175 |
| AF-W306 no else edge | 145 |
| AF-E303 endless cycle | 135 |
| workflows with no errors (code also checked) | 87 |
| disagreements | **0** |

### 3.5 Scale and performance

| Workflow | Result | Time |
|---|---|---:|
| 100 agents in a chain | compiles | 0.01 s |
| 1,000 agents in a chain | compiles | 0.35 s |
| 3,000 agents in a chain | compiles | 3.3 s |
| 150 agents, 450 edges, many bounded loops | compiles | 0.19 s |
| 3,000 `or` terms in one condition | compiles | |
| 100,000 nested parentheses | clean error AF-E103 | |

Time grows faster than linearly on very large graphs, because looking up a node's edges scans the whole edge list. This is irrelevant at the size of real workflows and keeps the code simple.

## 4. Bugs found and fixed

| # | Found by | Input | Wrong behaviour | Fix |
|---|---|---|---|---|
| 1 | edge cases | `n: int = ²` | **crash**: Python's `isdigit()` accepts `²` but `int()` rejects it | names and numbers are ASCII only (AF-E001) |
| 2 | edge cases | `agent x² { }` | generated Python had a syntax error | same fix |
| 3 | edge cases | 400 nested parentheses, 500 nested `if`s | **crash** (`RecursionError`) | parser nesting limit of 100 levels, error AF-E103 |
| 4 | performance test | a flat chain of 1,000 agents | **crash**: Tarjan's algorithm recurses once per node and hit Python's limit of 1,000 calls | recursion limit raised; deep *source* nesting is limited by #3 instead |
| 5 | edge cases | field named `class`, `None`, `from` | generated `class AgentState(TypedDict)` was not valid Python | state is now emitted as `TypedDict("AgentState", {...})`, which accepts any field name |
| 6 | edge cases | tool parameter named `from`, `lambda` | generated `def tool_t(from: str)` was invalid | keyword parameters get a trailing `_` (`from_`) |
| 7 | edge cases | field `_count_0` or `_next_x`; agent `__start__` | silent clash with the compiler's hidden fields or LangGraph's reserved node, breaking routing | names starting with `_` are reserved (AF-E213) |
| 8 | runtime tests | workflow whose agents change nothing | the generated file **crashed at runtime**: LangGraph reports such a step's update as `None` | the generated run loop handles `None` updates |

Each bug now has a regression test in `test_edge_cases.py`.

Also checked and found to be correct (not bugs): escapes in prompts (`\"`, `\\`, `\n`, `\t`) round trip into valid Python, and a real line break inside a string is reported as unterminated (AF-E002), as the grammar says.

## 5. Limits of this testing

- Generated workflows are run in **mock mode**. The tests prove the generated LangGraph code runs and routes as intended, not how a real LLM behaves. That matches the project's honest framing: the compiler verifies structure, not LLM output.
- Bounded loop counters count over the **whole run**. In nested loops, the inner loop's bound is shared across outer iterations. This is the documented behaviour (contract decision 4), and a test checks that separate loops each respect their own bound.
- The differential test uses workflows of up to 8 nodes, so enumerating every path stays fast. The algorithms do not depend on size, and larger graphs are covered by the scale tests above.
- The web UI was checked by hand through headless browser screenshots of every tab. It has no automated UI tests.
