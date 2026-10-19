# AgentForge contract

This file is the single source of truth shared by Person A (lexer, parser, API, UI) and Person B (semantic, IR, verify, optimize, codegen).
Do not change it without telling the other person.

---

## 1. AST JSON

Every node is a JSON object:

```json
{ "type": "<NodeType>", "line": 1, "col": 1, ...fields }
```

`line` and `col` are 1-based and point at the first token of the node.
Types are written as plain strings: `"string"`, `"int"`, `"bool"`, `"list[string]"`, `"list[list[int]]"`.

| Node | Fields |
|---|---|
| `Program` | `declarations`: list of `StateDecl \| ToolDecl \| AgentDecl \| FlowDecl`, in source order |
| `StateDecl` | `name`: str, `fields`: list of `FieldDecl` |
| `FieldDecl` | `name`: str, `modifier`: `"input" \| "output" \| null`, `field_type`: type string, `default`: `Literal`, a JSON list of `Literal` (for `= [...]`), or `null` |
| `ToolDecl` | `name`: str, `params`: list of `Param`, `return_type`: type string |
| `Param` | `name`: str, `param_type`: type string |
| `AgentDecl` | `name`: str, `body`: list of statements (`Assign \| If`) |
| `Assign` | `target`: `StateRef`, `value`: expression |
| `If` | `condition`: expression, `then_body`: list of statements, `else_body`: list of statements (empty if no else; `else if` is an `If` inside `else_body`) |
| `Literal` | `value`: str \| int \| bool, `value_type`: `"string" \| "int" \| "bool"` |
| `StateRef` | `field`: str |
| `Call` | `name`: str, `args`: list of expressions |
| `LlmCall` | `prompt`: str, `args`: list of expressions, `one_of`: list of `Literal` or `null` |
| `BinaryOp` | `op`: `"==" \| "!=" \| "<" \| ">" \| "<=" \| ">=" \| "and" \| "or"`, `left`, `right`: expressions |
| `UnaryOp` | `op`: `"not"`, `operand`: expression |
| `FlowDecl` | `name`: str, `edges`: list of `Edge` (chains already split into single edges) |
| `Edge` | `source`: str, `target`: str (`"start"` and `"end"` are the special nodes), `condition`: expression or `null`, `is_default`: bool (true for `-[else]->`), `max_iterations`: int or `null` |

### Small full example

Source:

```
state S {
  input q: string
  output a: string
}
agent answer {
  state.a = llm("Answer", state.q)
}
flow main {
  start -> answer -> end
}
```

AST:

```json
{
  "type": "Program", "line": 1, "col": 1,
  "declarations": [
    { "type": "StateDecl", "line": 1, "col": 1, "name": "S", "fields": [
      { "type": "FieldDecl", "line": 2, "col": 3, "name": "q", "modifier": "input", "field_type": "string", "default": null },
      { "type": "FieldDecl", "line": 3, "col": 3, "name": "a", "modifier": "output", "field_type": "string", "default": null }
    ]},
    { "type": "AgentDecl", "line": 5, "col": 1, "name": "answer", "body": [
      { "type": "Assign", "line": 6, "col": 3,
        "target": { "type": "StateRef", "line": 6, "col": 3, "field": "a" },
        "value": { "type": "LlmCall", "line": 6, "col": 13, "prompt": "Answer", "one_of": null,
                   "args": [ { "type": "StateRef", "line": 6, "col": 28, "field": "q" } ] } }
    ]},
    { "type": "FlowDecl", "line": 8, "col": 1, "name": "main", "edges": [
      { "type": "Edge", "line": 9, "col": 3, "source": "start", "target": "answer", "condition": null, "is_default": false, "max_iterations": null },
      { "type": "Edge", "line": 9, "col": 12, "source": "answer", "target": "end", "condition": null, "is_default": false, "max_iterations": null }
    ]}
  ]
}
```

The full AST of the research example is in `backend/tests/fixtures/ast/research.json`.

---

## 2. API

### `POST /api/compile`

Request body: `{ "source": "<.af program text>" }`

Response:

```json
{
  "ok": true,
  "phases": {
    "lexer":    { "status": "ok", "tokens": [{"type": "STATE", "lexeme": "state", "line": 1, "col": 1}] },
    "parser":   { "status": "ok", "ast": { "type": "Program", "...": "..." } },
    "semantic": { "status": "ok", "symbols": {
                    "state":  [{"name": "query", "type": "string", "modifier": "input"}],
                    "tools":  [{"name": "web_search", "params": [{"name": "query", "type": "string"}], "return_type": "list[string]"}],
                    "agents": [{"name": "classifier", "reads": ["query"], "writes": ["route"], "must_writes": ["route"]}] } },
    "ir":       { "status": "ok",
                  "nodes": [{"id": "classifier", "kind": "agent", "reads": [], "writes": [], "must_writes": [], "status": "ok", "line": 1}],
                  "edges": [{"from": "classifier", "to": "researcher", "condition": "state.route == \"factual\"", "max_iterations": null, "is_default": false}] },
    "verify":   { "status": "ok", "checks": [{"name": "Reachability", "status": "pass", "message": "...", "path": null}] },
    "optimize": { "status": "ok", "removed_agents": [] },
    "codegen":  { "status": "ok", "code": "..." }
  },
  "diagnostics": [
    {"severity": "error", "code": "AF-E201", "phase": "semantic", "message": "...", "line": 1, "col": 1, "path": null}
  ]
}
```

Field notes:

- `status` of a phase: `"ok"`, `"error"` or `"skipped"`. Warnings do not make a phase fail.
- `ok` is true when there are no error diagnostics at all (warnings allowed).
- If a phase has errors, every later phase is `"skipped"` (with empty data), but earlier phases' output is still returned so the UI can show partial results. Exception: when `verify` fails, `ir` is still returned (it was built before verify) so the graph can show red nodes.
- Token `type` is the name of the token kind (`IDENT`, `STRING`, `INT`, `ARROW`, a keyword name in capitals such as `STATE`, ...). The last token is always `EOF`.
- Agent `reads` = fields the agent reads **before** it has written them itself (so they must come from the caller or an earlier agent). `writes` = every field the agent might write. `must_writes` = fields written on every path through the agent body (an assignment inside only one branch of an `if` is in `writes` but not `must_writes`).
- IR node `kind`: `"start"`, `"agent"` or `"end"`. Node `status`: `"ok"`, `"warning"` or `"error"`, computed from the diagnostics about that node.
- IR edge `condition` is the condition re-printed as DSL text, `null` for plain edges and for `else` edges.
- Verify check `status`: `"pass"`, `"warn"` or `"fail"`. `path` is a witness path (list of node ids) or `null`.
- Diagnostic `path` is a witness path (list of node ids) or `null`.

### `GET /api/examples`

Returns `[{ "name": "research.af", "category": "valid" | "invalid", "source": "..." }]`, read from the `examples/` folder. Used by the UI examples dropdown.

---

## 3. Diagnostic codes

| Code | Phase | Meaning |
|---|---|---|
| AF-E001 | lexer | unexpected character |
| AF-E002 | lexer | unterminated string |
| AF-E101 | parser | unexpected token (expected X) |
| AF-E102 | parser | missing closing brace / bracket / parenthesis |
| AF-E103 | parser | nested too deeply (more than 100 levels of parentheses, `not` or `if` blocks) |
| AF-E201 | semantic | undeclared tool |
| AF-E202 | semantic | undeclared state field |
| AF-E203 | semantic | type mismatch |
| AF-E204 | semantic | unknown agent in flow |
| AF-E205 | semantic | duplicate declaration (field, tool, agent, parameter, second `state` or `flow`) |
| AF-E206 | semantic | wrong argument count or type in tool call |
| AF-E207 | semantic | condition is not boolean (also operands of `and`, `or`, `not`) |
| AF-W208 | semantic | edge compares a field against a value that is not in that field's `one_of` set |
| AF-E209 | semantic | missing required declaration (no `state`, no `agent`, or no `flow`) |
| AF-E210 | semantic | invalid use of `start` / `end` (edge into `start` or out of `end`) |
| AF-E211 | semantic | tool or `llm` call inside an edge condition (routing must only read state) |
| AF-E212 | semantic | ambiguous outgoing edges (more than one plain edge, a plain edge mixed with conditional edges, or more than one `else`) |
| AF-E213 | semantic | name starting with `_` (reserved for the compiler's hidden fields and LangGraph's `__start__` / `__end__`) |
| AF-W301 | verify | unreachable (dead) agent |
| AF-E302 | verify | agent (or an edge condition) reads a field that is not guaranteed to be written on every path to it |
| AF-E303 | verify | cycle with no exit edge and no `max_iterations` bound |
| AF-E304 | verify | an `output` field is not guaranteed to be written on some path to `end` |
| AF-E305 | verify | node cannot reach `end` |
| AF-W306 | verify | conditional edges from a node have no `else` edge |

---

## 4. Decisions made to fill gaps in the spec

1. **CLI**: `cd backend` then `python -m compiler file.af [-o out.py]`. The compiler package is `backend/compiler/` (renamed from the spec's `backend/agentforge/` to avoid a confusing agentforge/agentforge path). The CLI code lives in `compiler/cli.py` and `compiler/__main__.py` (instead of `backend/cli.py`) because `python -m compiler` requires `__main__.py` inside the package.
2. **`llm(...)` type**: returns `string`; with `one_of [...]` it returns the type of the listed literals (all must have the same type).
3. **Comparisons**: `==` / `!=` need both sides of the same type; `< > <= >=` need two `int`s.
4. **Bounded loop semantics**: each edge with `max_iterations N` gets a hidden counter in the generated state. The edge is taken only while its condition is true **and** it has been taken fewer than N times. When the bound is hit, routing falls through to the node's other edges (normally the `else` edge).
5. **No matching edge at runtime**: if a node has conditional edges but no `else` (warning AF-W306) and no condition matches, the generated code raises a clear `RuntimeError`. It does not silently stop.
6. **State completeness** uses `must_writes` (definite writes). The fixpoint starts every node except `start` at "all fields" and shrinks; this handles loops without infinite recursion. Edges from unreachable agents are ignored.
7. **Cycle rule**: a cycle (strongly connected component) is accepted if one of its edges has `max_iterations` or one edge leaves it. Otherwise AF-E303. Nodes in such a cycle do not also get AF-E305.
8. **Dead agents**: verification warns (AF-W301), then the optimizer removes them before codegen and lists them in `removed_agents`.
9. **State field defaults**: `name: type = value` (a literal, or `[...]` of literals for lists). A field with a default is written at `start`, like an `input` field. Added because the spec's example reads `state.context` on a path where nothing writes it. The spec's original version is kept as `examples/invalid/read_before_write.af`.
10. **Lexer errors stop the pipeline**: if the lexer reports errors, the parser is skipped (the spec rule "later phases are skipped").
