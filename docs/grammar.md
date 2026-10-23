# AgentForge grammar (EBNF)

This is the grammar the hand written recursive descent parser follows.
Each rule below is one function in `backend/compiler/parser.py`.
The grammar is LL(1): at every choice point the next token alone decides which branch to take.

Notation: `{ x }` = zero or more, `[ x ]` = optional, `|` = choice, `"x"` = keyword or symbol.

```ebnf
program      = { declaration } EOF ;
declaration  = state_decl | tool_decl | agent_decl | flow_decl ;

(* state schema *)
state_decl   = "state" IDENT "{" { field_decl } "}" ;
field_decl   = [ "input" [ "untrusted" ] | "output" ] IDENT ":" type [ "=" default ] ;
default      = literal | "[" [ literal { "," literal } ] "]" ;
type         = "string" | "int" | "bool" | "list" "[" type "]" ;

(* tools *)
tool_decl    = "tool" IDENT "(" [ param { "," param } ] ")" "->" type { tool_tag } ;
tool_tag     = "untrusted" | "sensitive" | "sanitizer" ;
param        = IDENT ":" type ;

(* agents *)
agent_decl   = "agent" IDENT block ;
block        = "{" { statement } "}" ;
statement    = assign | if_stmt ;
assign       = state_ref "=" expr ;
if_stmt      = "if" "(" expr ")" block [ "else" ( block | if_stmt ) ] ;

(* flow *)
flow_decl    = "flow" IDENT "{" { edge_chain } "}" ;
edge_chain   = node arrow node { arrow node } ;
node         = IDENT | "start" | "end" ;
arrow        = "->"
             | "-[" edge_label "]" "->" ;
edge_label   = "else"
             | expr [ "," "max_iterations" INT ] ;

(* expressions, lowest precedence first: or < and < not < comparison *)
expr         = or_expr ;
or_expr      = and_expr { "or" and_expr } ;
and_expr     = not_expr { "and" not_expr } ;
not_expr     = "not" not_expr | comparison ;
comparison   = primary [ comp_op primary ] ;
comp_op      = "==" | "!=" | "<" | ">" | "<=" | ">=" ;
primary      = literal
             | state_ref
             | tool_call
             | llm_call
             | "(" expr ")" ;
state_ref    = "state" "." IDENT ;
tool_call    = IDENT "(" [ expr { "," expr } ] ")" ;
llm_call     = "llm" "(" STRING { "," expr } ")" [ "one_of" "[" literal { "," literal } "]" ] ;
literal      = STRING | INT | "true" | "false" ;
```

## Tokens

| Token | Pattern |
|---|---|
| IDENT | ASCII letter or `_`, then ASCII letters, digits, `_` (not a keyword) |
| INT | one or more ASCII digits |
| STRING | `"..."` on one line, escapes `\"` `\\` `\n` `\t` |
| keywords | `state tool agent flow start end input output llm one_of if else and or not true false max_iterations string int bool list untrusted sensitive sanitizer compare candidates case expect contains mock end_to_end ensure necessity` |
| symbols | `{ } ( ) [ ] , : . = == != < > <= >= -> -[` |
| comment | `#` to end of line (skipped) |

## Notes and small decisions

- A state field may have a default value (`context: list[string] = []`). A field with a default counts as written at `start`. This was added because in the spec's example `responder` reads `state.context`, which is never written on the `else` path. Without a default, the compiler correctly reports that as AF-E302.
- `-[` is one token (the opening of a labelled edge). The closing is the two tokens `]` and `->`, so `]->` and `] ->` both work.
- Comparisons do not chain: `a == b == c` is a syntax error. This keeps the grammar simple and avoids confusing meaning.
- A bare name is never a value. The only thing an `IDENT` can start inside an expression is a tool call `name(...)`. State is always accessed as `state.field`.
- Edge chains are sugar: `a -> b -[c]-> d` becomes the two edges `a -> b` and `b -[c]-> d`.
- There is no minus operator and no negative number literal (not needed by the language).
- Panic mode recovery: on a syntax error the parser records it, skips tokens until the start of the next declaration (`tool`, `agent`, `flow`, or `state` followed by a name), and continues. That is how one run reports several syntax errors.

## Comparing agents (`compare`)

```ebnf
declaration  = state_decl | tool_decl | agent_decl | flow_decl | compare_decl ;

compare_decl = "compare" IDENT "{" "candidates" IDENT { "," IDENT }
               { "end_to_end" | "ensure" expr }
               { case } "}" ;
case         = "case" "{" { case_item } "}" ;
case_item    = state_ref "=" default          (* the state this test case starts from *)
             | "expect" state_ref ( "contains" | "==" ) literal
             | "mock" IDENT STRING ;          (* the scripted answer of one candidate, used in mock mode *)
```

New keywords: `compare candidates case expect contains mock end_to_end ensure necessity`.

- Candidates are ordinary agents. The ones that are not used in the `flow` wait outside it; they are not reported as dead agents.
- Each case needs exactly one `expect`. `contains` is for string fields and ignores letter case; `==` is an exact match.
- A `mock` line gives the answer a candidate "returns" in mock mode. With a real model (`AGENTFORGE_LLM=real` in the generated file) the scripts are ignored.
- `end_to_end`: judge each candidate by the FINAL output of the whole workflow (a case runs the entire flow with that candidate in the wired place) instead of by the candidate's own output. The `expect` field may then be written by a later agent (so AF-E215 does not apply). Needs exactly one candidate in the flow (AF-E217).
- `ensure <condition>`: a run-time check on the wired agent, an ordinary bool expression over state (no calls). With a real model, an answer that fails it makes the run try the other candidates in order, and stop with an error if none passes. It does not apply to mock answers. Needs exactly one candidate in the flow (AF-E217).
- `compare` is a top level declaration, so panic mode recovery also stops at it.

## Which agents are needed? (`necessity`)

```ebnf
declaration   = state_decl | tool_decl | agent_decl | flow_decl | compare_decl | necessity_decl ;
necessity_decl = "necessity" IDENT "{" { case } "}" ;      (* `case` as in a compare block, without `mock` lines *)
```

- A necessity block is only test cases: state the workflow starts from and one `expect` on the final output. There is at most one per program (AF-E218), and `mock` lines are not allowed (the real workflow runs).
- With a real model the compiler runs the whole workflow on every case, once as written and once with each testable agent left out. An agent whose removal does not make the final output worse is not needed and is removed from the generated code.
- Which agents are testable is decided statically (`necessity.py`): agents that call a tool, choose the next step (conditional edges), are compare candidates, or that verification needs (skipping them would break it) are never removed.
